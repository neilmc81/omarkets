import json
from datetime import UTC, date, datetime

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api import desk as desk_api
from core.desk.identity import NQ
from core.desk.models import NoteKind
from core.desk.writer import DeskWriter
from core.providers.earnings_nasdaq import parse
from core.services.ai_budget import BudgetPaused

GOOD = {
    "headline": "Factory output beat forecasts",
    "summary": "Stronger growth, slightly higher yields.",
    "what_it_means": "The economy produced more than expected.",
    "market_reaction": "NQ slipped 0.18%.",
    "rates_read": "The 2-year yield rose 0.6bp.",
    "watch_next": ["Jobless claims Thursday"],
    "learn": "Industrial production measures factory output.",
    "nq_tone": "mixed",
}


#: Facts holding the numbers GOOD quotes, so the number guardrail passes.
FACTS = {"nq": "-0.18%", "two_year": "+0.6bp"}


class FakeGuard:
    def __init__(self, paused=False):
        self.paused = paused
        self.reserved: list[float] = []
        self.settled: list[tuple[str, float]] = []

    def reserve(self, provider, model, max_cost_usd, *, priority=False):
        if self.paused:
            raise BudgetPaused("Weekly Omarkets AI budget cannot cover the next request.")
        self.reserved.append(max_cost_usd)
        return "token"

    def settle(self, token, actual):
        self.settled.append((token, actual))


def writer_with(handler, guard=None):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return DeskWriter(api_key="k", model="anthropic/claude-sonnet-5.5", input_rate=3.0,
                      output_rate=15.0, cost_guard=guard, client=client)


def completion(content, cost=0.012):
    return httpx.Response(200, json={
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 3000, "completion_tokens": 600, "cost": cost},
    })


async def test_valid_output_is_parsed_and_cost_settled():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers["authorization"]
        return completion("```json\n" + json.dumps(GOOD) + "\n```")

    guard = FakeGuard()
    result = await writer_with(handler, guard).write(
        NoteKind.RELEASE, {"figures": [], **FACTS}, instrument=None)
    assert result.draft is not None and result.draft.nq_tone == "mixed"
    assert result.cost_usd == 0.012
    assert guard.settled == [("token", 0.012)]
    assert 0 < guard.reserved[0] < 0.1
    assert seen["auth"] == "Bearer k"
    assert seen["body"]["messages"][0]["role"] == "system"
    assert "FACTS" in seen["body"]["messages"][1]["content"]


async def test_trading_advice_is_rejected():
    bad = {**GOOD, "summary": "Traders should go long here."}
    result = await writer_with(lambda r: completion(json.dumps(bad))).write(
        NoteKind.MOVE, {}, instrument=NQ)
    assert result.draft is None and result.error == "draft contained trading advice"


async def test_invalid_output_and_http_errors():
    result = await writer_with(lambda r: completion("not json")).write(
        NoteKind.MOVE, {}, instrument=NQ)
    assert result.draft is None and result.error == "model output was not valid"

    guard = FakeGuard()
    result = await writer_with(lambda r: httpx.Response(401), guard).write(
        NoteKind.MOVE, {}, instrument=NQ)
    assert result.error == "provider returned HTTP 401"
    assert guard.settled == [("token", 0.0)]


async def test_budget_pause_blocks_the_call():
    called = []

    def handler(request):
        called.append(request)
        return completion(json.dumps(GOOD))

    result = await writer_with(handler, FakeGuard(paused=True)).write(
        NoteKind.MOVE, {}, instrument=NQ)
    assert result.draft is None and result.error.startswith("AI budget:")
    assert called == []


async def test_missing_key_is_reported_not_called():
    writer = DeskWriter(api_key=None, model="m", input_rate=1, output_rate=1)
    assert not writer.ready
    result = await writer.write(NoteKind.ASK, {}, instrument=NQ)
    assert result.error == "OPENROUTER_API_KEY is not configured"


def test_earnings_parser_keeps_mega_caps_and_validates():
    payload = {"data": {"rows": [
        {"symbol": "NFLX", "time": "time-after-hours", "epsForecast": "$0.82",
         "fiscalQuarterEnding": "Sep/2026"},
        {"symbol": "ZZZZ", "time": "time-pre-market", "epsForecast": "$1.00"},
        {"symbol": "TSLA", "time": "time-not-supplied", "epsForecast": "($0.05)"},
        "garbage",
    ]}}
    rows = parse(payload, date(2026, 10, 20))
    assert [r.symbol for r in rows] == ["NFLX", "TSLA"]
    assert rows[0].timing == "after_close" and rows[0].eps_forecast == 0.82
    assert rows[0].expected_at == datetime(2026, 10, 20, 20, 5, tzinfo=UTC)
    assert rows[1].timing == "unknown" and rows[1].eps_forecast == -0.05
    assert parse(None, date(2026, 10, 20)) == []


def test_desk_api_returns_503_when_disabled():
    app = FastAPI()
    app.include_router(desk_api.router)
    app.state.desk = None
    client = TestClient(app)
    assert client.get("/api/v1/desk").status_code == 503
    assert client.post("/api/v1/desk/ask").status_code == 503


async def test_raw_newlines_inside_strings_are_accepted():
    raw = json.dumps(GOOD).replace("The economy produced", "The economy\nproduced")
    assert "\n" in raw
    result = await writer_with(lambda r: completion(raw)).write(
        NoteKind.RELEASE, {}, instrument=None)
    assert result.draft is not None and "\n" in result.draft.what_it_means


async def test_truncated_output_is_logged_with_its_token_use(caplog):
    """Oct 20, 2026: a premarket note was cut off at the output ceiling. The log
    must say how the ceiling was spent, and the ceiling leaves room to reason."""
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(GOOD)[:120]},
                         "finish_reason": "length"}],
            "usage": {"prompt_tokens": 3000, "completion_tokens": 4000, "cost": 0.07,
                      "completion_tokens_details": {"reasoning_tokens": 3500}},
        })

    with caplog.at_level("WARNING", logger="core.desk.writer"):
        result = await writer_with(handler).write(NoteKind.PREMARKET, {}, instrument=NQ)
    assert result.error == "model output was not valid"
    assert seen["body"]["max_tokens"] >= 4000
    assert "finish_reason=length" in caplog.text
    assert "reasoning_tokens=3500" in caplog.text


# --- per-kind models, DeepSeek routing, Opus fallback, number guardrail (#6) ---

from core.config.settings import (  # noqa: E402
    DEEPSEEK,
    DEEPSEEK_PROVIDER,
    OPUS,
    Settings,
)
from core.desk.models import NoteDraft  # noqa: E402
from core.desk.writer import invented_numbers, reserve_pad  # noqa: E402


def desk_settings(**env):
    return Settings(_env_file=None, **env)


def split_writer(handler, guard=None, settings=None):
    settings = settings or desk_settings()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return DeskWriter(
        api_key="k", models={k: settings.desk_model_for(k) for k in NoteKind},
        fallback_model=settings.desk_fallback_model,
        model_params=settings.resolved_desk_model_params,
        model_rates=settings.resolved_desk_model_rates,
        cost_guard=guard, client=client,
    )


def test_default_model_map():
    settings = desk_settings()
    for kind in NoteKind:
        expected = OPUS if kind in {NoteKind.REVIEW, NoteKind.PREMARKET} else DEEPSEEK
        assert settings.desk_model_for(kind) == expected
    assert settings.desk_fallback_model == OPUS


def test_env_overrides_and_precedence(monkeypatch):
    monkeypatch.setenv("OMARKETS_DESK_MODEL", "anthropic/claude-sonnet-5.5")
    monkeypatch.setenv("OMARKETS_DESK_MODELS", '{"move": "x/cheap"}')
    monkeypatch.setenv("OMARKETS_DESK_MODEL_RATES", '{"x/cheap": [0.1, 0.2]}')
    settings = Settings(_env_file=None)
    assert settings.desk_model_for(NoteKind.MOVE) == "x/cheap"  # per-kind wins
    assert settings.desk_model_for(NoteKind.REVIEW) == "anthropic/claude-sonnet-5.5"
    rates = settings.resolved_desk_model_rates
    assert rates["x/cheap"] == (0.1, 0.2) and rates[DEEPSEEK] == (0.15, 0.60)


async def test_deepseek_request_carries_pinned_provider_routing():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return completion(json.dumps(GOOD), cost=0.0008)

    result = await split_writer(handler).write(NoteKind.MOVE, {}, instrument=NQ)
    assert result.model == DEEPSEEK and result.draft is not None
    assert bodies[0]["model"] == DEEPSEEK
    assert bodies[0]["provider"] == DEEPSEEK_PROVIDER
    assert bodies[0]["provider"]["allow_fallbacks"] is False
    assert bodies[0]["max_tokens"] >= 4000

    bodies.clear()
    await split_writer(handler).write(NoteKind.REVIEW, {}, instrument=None)
    assert bodies[0]["model"] == OPUS and "provider" not in bodies[0]


async def test_deepseek_failure_falls_back_to_opus():
    models = []

    def handler(request):
        body = json.loads(request.content)
        models.append(body["model"])
        if body["model"] == DEEPSEEK:
            return completion("not json", cost=0.0005)
        return completion(json.dumps(GOOD), cost=0.06)

    guard = FakeGuard()
    result = await split_writer(handler, guard).write(NoteKind.RELEASE, FACTS, instrument=None)
    assert models == [DEEPSEEK, OPUS]
    assert result.draft is not None and result.model == OPUS
    assert result.cost_usd == 0.0605
    assert guard.settled == [("token", 0.0005), ("token", 0.06)]


async def test_http_and_advice_failures_also_fall_back():
    for failure in (lambda: httpx.Response(503),
                    lambda: completion(json.dumps({**GOOD, "summary": "Go long now."}))):
        def handler(request, failure=failure):
            if json.loads(request.content)["model"] == DEEPSEEK:
                return failure()
            return completion(json.dumps(GOOD))

        result = await split_writer(handler).write(NoteKind.ASK, {}, instrument=NQ)
        assert result.model == OPUS and result.draft is not None


async def test_budget_cap_blocks_the_fallback():
    class CapGuard(FakeGuard):
        def reserve(self, provider, model, max_cost_usd, *, priority=False):
            if model == OPUS:
                raise BudgetPaused("Weekly Omarkets AI budget cannot cover the next request.")
            return super().reserve(provider, model, max_cost_usd, priority=priority)

    models = []

    def handler(request):
        models.append(json.loads(request.content)["model"])
        return httpx.Response(500)

    result = await split_writer(handler, CapGuard()).write(NoteKind.MOVE, {}, instrument=NQ)
    assert models == [DEEPSEEK]
    assert result.draft is None and result.model == DEEPSEEK
    assert "fallback" in result.error and "AI budget" in result.error


async def test_paused_budget_skips_fallback_entirely():
    result = await split_writer(lambda r: completion(json.dumps(GOOD)),
                                FakeGuard(paused=True)).write(NoteKind.MOVE, {}, instrument=NQ)
    assert result.error.startswith("AI budget:")


async def test_each_model_is_priced_at_its_own_rates():
    guard = FakeGuard()

    def handler(request):  # no usage.cost: fall back to per-model rates
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(GOOD)}}],
            "usage": {"prompt_tokens": 3000, "completion_tokens": 2000},
        })

    deepseek = await split_writer(handler, guard).write(NoteKind.MOVE, FACTS, instrument=NQ)
    opus = await split_writer(handler, guard).write(NoteKind.REVIEW, FACTS, instrument=None)
    assert deepseek.cost_usd == pytest.approx((3000 * 0.15 + 2000 * 0.60) / 1e6)
    assert opus.cost_usd == pytest.approx((3000 * 5 + 2000 * 25) / 1e6)
    # The worst-case reservation follows the model too.
    assert guard.reserved[0] < 0.01 < guard.reserved[1]


def test_deepseek_reserves_twice_the_estimate_and_others_keep_20_percent():
    assert reserve_pad(DEEPSEEK) == 2.0
    assert reserve_pad("deepseek/some-other-deepseek") == 2.0
    assert reserve_pad(OPUS) == 1.2
    assert reserve_pad("anthropic/claude-sonnet-5.5") == 1.2
    assert reserve_pad("x/cheap") == 1.2


@pytest.mark.parametrize(("model", "kind", "instrument", "rates", "pad"), [
    (DEEPSEEK, NoteKind.MOVE, NQ, (0.15, 0.60), 2.0),
    (OPUS, NoteKind.REVIEW, None, (5.0, 25.0), 1.2),
])
async def test_reservation_is_the_estimate_times_the_model_pad(model, kind, instrument,
                                                               rates, pad):
    guard = FakeGuard()
    writer = split_writer(lambda r: completion(json.dumps(GOOD), cost=0.0001), guard)
    prompt = writer.prompt(kind, FACTS, instrument=instrument)
    tokens_in = (len(prompt.system) + len(prompt.user)) / 3 + 200
    estimate = (tokens_in * rates[0] + 4000 * rates[1]) / 1e6
    bound = writer.max_cost(kind, FACTS, model=model, instrument=instrument)
    assert bound == pytest.approx(estimate * pad)
    await writer.write(kind, FACTS, instrument=instrument)
    assert guard.reserved[0] == pytest.approx(bound)


def test_unlisted_model_keeps_the_20_percent_margin():
    writer = writer_with(lambda r: completion(json.dumps(GOOD)))
    prompt = writer.prompt(NoteKind.RELEASE, FACTS, instrument=None)
    tokens_in = (len(prompt.system) + len(prompt.user)) / 3 + 200
    bound = writer.max_cost(NoteKind.RELEASE, FACTS, model="anthropic/claude-sonnet-5.5",
                            instrument=None)
    assert bound == pytest.approx((tokens_in * 3.0 + 4000 * 15.0) / 1e6 * 1.2)


def test_invented_numbers_allows_facts_and_their_roundings():
    draft = NoteDraft.model_validate({
        **GOOD, "market_reaction": "NQ fell 0.18% to 21,050.", "rates_read": "Up 4bp."})
    prompt = 'NQ change -0.183% level 21050.25 2-year +4.0bp industrial 0.6 3 1'
    assert invented_numbers(draft, prompt) == []
    assert invented_numbers(draft, "nothing here") == ["0.18", "21,050", "4"]


async def test_invented_numbers_regenerate_once_then_flag():
    bad = {**GOOD, "market_reaction": "NQ slipped 0.18% after a 777 point swing."}
    replies = [bad, GOOD]

    def handler(request):
        return completion(json.dumps(replies.pop(0)) if replies else json.dumps(bad))

    facts = FACTS
    result = await split_writer(handler).write(NoteKind.MOVE, facts, instrument=NQ)
    assert result.draft.market_reaction == GOOD["market_reaction"]
    assert result.unverified_numbers == ()

    result = await split_writer(lambda r: completion(json.dumps(bad))).write(
        NoteKind.MOVE, facts, instrument=NQ)
    assert result.draft is not None and result.unverified_numbers == ("777",)
    assert result.cost_usd == pytest.approx(0.024)
