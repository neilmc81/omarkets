"""The desk writer: one OpenRouter call per note, admitted by the AI cost guard."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx
from pydantic import ValidationError

from core.desk.identity import Instrument
from core.desk.models import NoteDraft, NoteKind
from core.desk.prompts import (
    PROMPT_VERSION,
    NoPromptProfile,
    Profile,
    Prompt,
    profiles_at,
    render,
)
from core.services.ai_budget import BudgetPaused

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
TEMPERATURE = 0.3

#: Phrases that would turn an explanation into trading advice.
_ADVICE = re.compile(
    r"\b(buy(ing)? (the dip|nq|calls|puts|now)|sell(ing)? (nq|calls|puts|now)|"
    r"go(ing)? (long|short)|stop[- ]loss|price target|take[- ]profit|entry point)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class WriteResult:
    draft: NoteDraft | None
    model: str
    cost_usd: float | None = None
    error: str | None = None
    #: Numbers in the draft found nowhere in the prompt (see ``invented_numbers``).
    unverified_numbers: tuple[str, ...] = ()
    #: The request as sent; ``None`` when none could be built (no prompt profile).
    prompt: Prompt | None = None


def _json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in model output")
    # strict=False accepts raw newlines inside strings, which models sometimes emit.
    return json.loads(text[start:end + 1], strict=False)


def _draft_text(draft: NoteDraft) -> str:
    return " ".join([draft.headline, draft.summary, draft.what_it_means,
                     draft.market_reaction, draft.rates_read or "", draft.learn, *draft.watch_next])


def check_draft(draft: NoteDraft, *, rates_read_required: bool = True) -> str | None:
    if _ADVICE.search(_draft_text(draft)):
        return "draft contained trading advice"
    # The reader accepts a missing rates_read; a prompt that asks for it (every
    # v8 prompt, and v9 wherever yields matter) treats a draft without it as invalid.
    if rates_read_required and draft.rates_read is None:
        return "model output was not valid"
    return None


_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
#: Digits that are part of a name, not a figure: Treasury maturities ("2-year",
#: "10y") and index names ("Nasdaq 100", "S&P 500", "FTSE 100"). Only these
#: exact names: "Nasdaq 777" or "a 77-year record" are still checked.
_NAMED = re.compile(
    r"(?<![\w.])(?:1|2|3|5|7|10|20|30)(?:-year|y)\b"
    r"|\b(?:Nasdaq[ -]100|S&P 500|Russell 2000|FTSE 100|DAX 40|CAC 40|Stoxx 600"
    r"|Stoxx 50|IBEX 35)(?![\d%]|[.,]\d)",
    re.IGNORECASE)
#: Request fields that change how many tokens a call may use.
_TOKEN_LIMITS = frozenset({"max_tokens", "max_completion_tokens", "max_output_tokens"})


def _number_forms(token: str) -> set[str]:
    """``token`` and every rounding of it to fewer decimals ("0.183" -> 0.18, 0.2, 0)."""
    value = Decimal(token.replace(",", ""))
    places = max(0, -value.as_tuple().exponent)
    return {format(value.quantize(Decimal(1).scaleb(-p), ROUND_HALF_UP).normalize(), "f")
            for p in range(places + 1)}


def invented_numbers(draft: NoteDraft, facts: str) -> list[str]:
    """Numbers in the draft that are not in ``facts``, or a rounding of one.

    ``facts`` is ``Prompt.number_sources``: the FACTS block plus the few facts
    the prompt states (the 10-minute delay), never the whole prompt, whose
    limits and examples ("max 45 words", rule numbers) are not facts.
    Digits inside names ("2-year", "Nasdaq 100") are not figures and are skipped.
    """
    allowed: set[str] = set()
    for match in _NUMBER.finditer(facts):
        allowed |= _number_forms(match.group())
    text = _NAMED.sub(" ", _draft_text(draft))
    return sorted({m.group() for m in _NUMBER.finditer(text)
                   if format(Decimal(m.group().replace(",", "")).normalize(), "f") not in allowed})


def _total(*costs: float | None) -> float | None:
    known = [c for c in costs if c is not None]
    return sum(known) if known else None


class DeskWriter:
    def __init__(
        self,
        *,
        api_key: str | None,
        models: Mapping[NoteKind, str] | None = None,
        #: Writes every kind missing from ``models``.
        model: str | None = None,
        #: Retried once when the first model fails; ``None`` disables the retry.
        fallback_model: str | None = None,
        #: Extra request-body fields per model (e.g. OpenRouter provider routing).
        model_params: Mapping[str, Mapping[str, Any]] | None = None,
        #: USD per million (input, output) tokens per model.
        model_rates: Mapping[str, tuple[float, float]] | None = None,
        #: Rates for models missing from ``model_rates``.
        input_rate: float = 3.0,
        output_rate: float = 15.0,
        cost_guard: Any = None,
        # A note is ~600 output tokens, but a model that reasons spends part of
        # this ceiling before writing: at 2,500 the Oct 20 premarket note was cut
        # off after 2,023 characters (finish_reason=length). DeepSeek reasons for
        # ~1.8k tokens per note. Billing is by use.
        max_output_tokens: int = 4000,
        timeout_seconds: float = 90.0,
        client: httpx.AsyncClient | None = None,
        url: str = OPENROUTER_URL,
        #: The prompt version every request is rendered at.
        prompt_version: str = PROMPT_VERSION,
        #: Prompt profiles by instrument (default: the version's). Tests only.
        profiles: Mapping[str, Profile] | None = None,
    ) -> None:
        profiles_at(prompt_version)  # an unknown version fails here, not per note
        if models is None and model is None:
            raise ValueError("DeskWriter needs models or model")
        self._api_key = api_key
        self._models = dict(models or {})
        self._model = model
        self._fallback = fallback_model or None
        self._params = {k: dict(v) for k, v in (model_params or {}).items()}
        self._rates = dict(model_rates or {})
        self._default_rates = (input_rate, output_rate)
        self._guard = cost_guard
        self._max_output = max_output_tokens
        self._timeout = timeout_seconds
        self._client = client
        self._url = url
        self._profiles = profiles
        self.prompt_version = prompt_version

    @property
    def ready(self) -> bool:
        return bool(self._api_key)

    @property
    def detail(self) -> str | None:
        return None if self.ready else "OPENROUTER_API_KEY is not configured"

    def model_for(self, kind: NoteKind) -> str:
        model = self._models.get(kind) or self._model
        if model is None:
            raise ValueError(f"no desk model for {kind}")
        return model

    def prompt(self, kind: NoteKind, facts: dict[str, Any], *,
               instrument: Instrument | None) -> Prompt:
        """The exact request for this note; ``NoPromptProfile`` if it can't be built."""
        return render(kind, instrument, facts, version=self.prompt_version,
                      profiles=self._profiles)

    def envelope(self, kind: NoteKind, result: WriteResult, *,
                 instrument: Instrument | None) -> dict[str, Any]:
        """How a note was requested: enough to replay its facts through the same setup.

        The hashes are of the system text and task actually sent (none when no
        request could be built)."""
        primary = self.model_for(kind)
        prompt = result.prompt
        envelope: dict[str, Any] = {
            "prompt_version": prompt.version if prompt else self.prompt_version,
            "system_sha256": prompt.system_sha256 if prompt else None,
            "task_sha256": prompt.task_sha256 if prompt else None,
            "temperature": TEMPERATURE,
            "primary_model": primary,
            "fallback_model": self._fallback,
            "model": result.model,
            #: Routing of the model that wrote the note (the fallback's, if it did).
            "model_params": self._params.get(result.model, {}),
            "max_output_tokens": self._max_output,
        }
        if instrument is not None:
            envelope["instrument"] = instrument.symbol
        return envelope

    def _rates_for(self, model: str) -> tuple[float, float]:
        return self._rates.get(model, self._default_rates)

    def max_cost(self, kind: NoteKind, facts: dict[str, Any], *, model: str,
                 instrument: Instrument | None) -> float:
        """The most one call for this note can cost (what the cost guard reserves)."""
        return self._bound(model, self.prompt(kind, facts, instrument=instrument))

    def hard_max_cost(self, kind: NoteKind, facts: dict[str, Any], *, model: str,
                      instrument: Instrument | None) -> float | None:
        """A ceiling for one call, not an estimate, at this model's recorded rates.

        No token is shorter than one UTF-8 byte (plus a fixed allowance for chat
        formatting), and the provider stops at ``max_tokens``. ``None`` when the
        model's routing params could change that limit.
        """
        if _TOKEN_LIMITS & self._params.get(model, {}).keys():
            return None
        prompt = self.prompt(kind, facts, instrument=instrument)
        rate_in, rate_out = self._rates_for(model)
        tokens_in = len((prompt.system + prompt.user).encode()) + 500
        return (tokens_in * rate_in + self._max_output * rate_out) / 1e6

    def _bound(self, model: str, prompt: Prompt) -> float:
        # ~3 characters per token is conservative for English/JSON; +20% margin.
        rate_in, rate_out = self._rates_for(model)
        tokens_in = (len(prompt.system) + len(prompt.user)) / 3 + 200
        return (tokens_in * rate_in + self._max_output * rate_out) / 1e6 * 1.2

    def _prepare(self, kind: NoteKind, facts: dict[str, Any], instrument: Instrument | None,
                 model: str) -> Prompt | WriteResult:
        """The request, or a failed result when there is no prompt for this instrument.
        Nothing is reserved or sent for a refused note."""
        if not self.ready:
            return WriteResult(None, model, error=self.detail)
        try:
            return self.prompt(kind, facts, instrument=instrument)
        except NoPromptProfile as exc:
            logger.warning("desk writer refused %s: %s", kind.value, exc)
            return WriteResult(None, model, error=str(exc))

    async def write(self, kind: NoteKind, facts: dict[str, Any], *,
                    instrument: Instrument | None) -> WriteResult:
        primary = self.model_for(kind)
        prompt = self._prepare(kind, facts, instrument, primary)
        if isinstance(prompt, WriteResult):
            return prompt
        result = await self._call(primary, prompt)
        spent = [result.cost_usd]
        if (result.draft is None and self._fallback and self._fallback != primary
                and not (result.error or "").startswith("AI budget")):
            logger.warning("desk writer %s failed (%s); retrying on %s",
                           primary, result.error, self._fallback)
            retry = await self._call(self._fallback, prompt)
            spent.append(retry.cost_usd)
            result = retry if retry.draft else WriteResult(
                None, primary, error=f"{result.error}; fallback {self._fallback}: {retry.error}")
        if result.draft is not None:
            checked = prompt.number_sources
            invented = invented_numbers(result.draft, checked)
            if invented:
                logger.warning("desk writer %s invented numbers %s; regenerating",
                               result.model, invented)
                again = await self._call(result.model, prompt)
                spent.append(again.cost_usd)
                if again.draft is not None:
                    second = invented_numbers(again.draft, checked)
                    if len(second) <= len(invented):
                        result, invented = again, second
                if invented:
                    logger.warning("desk note flagged: unverified numbers %s", invented)
                result = replace(result, unverified_numbers=tuple(invented))
        return replace(result, cost_usd=_total(*spent), prompt=prompt)

    async def attempt(self, kind: NoteKind, facts: dict[str, Any], *,
                      instrument: Instrument | None, model: str | None = None) -> WriteResult:
        """Exactly one call, for the evaluation runner: no fallback, no regeneration.

        Invented numbers are flagged on the result, never retried.
        """
        model = model or self.model_for(kind)
        prompt = self._prepare(kind, facts, instrument, model)
        if isinstance(prompt, WriteResult):
            return prompt
        result = replace(await self._call(model, prompt), prompt=prompt)
        if result.draft is None:
            return result
        return replace(result, unverified_numbers=tuple(
            invented_numbers(result.draft, prompt.number_sources)))

    async def _call(self, model: str, prompt: Prompt) -> WriteResult:
        token = None
        if self._guard is not None:
            try:
                token = self._guard.reserve(
                    "openrouter", model, self._bound(model, prompt), priority=True,
                )
            except BudgetPaused as exc:
                return WriteResult(None, model, error=f"AI budget: {exc}")
        body = {
            "model": model,
            "max_tokens": self._max_output,
            "temperature": TEMPERATURE,
            "response_format": {"type": "json_object"},
            "usage": {"include": True},
            "messages": [
                {"role": "system", "content": prompt.system},
                {"role": "user", "content": prompt.user},
            ],
            **self._params.get(model, {}),
        }
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        cost: float | None = None
        try:
            try:
                response = await client.post(
                    self._url, json=body,
                    headers={"Authorization": f"Bearer {self._api_key}",
                             "X-Title": "Omarkets"},
                )
            except httpx.HTTPError as exc:
                # Unknown whether the request was billed; the reservation stays held.
                return WriteResult(None, model, error=f"network error ({type(exc).__name__})")
            if response.status_code >= 400:
                if token is not None and response.status_code in {400, 401, 402, 403, 404}:
                    self._guard.settle(token, 0.0)
                return WriteResult(None, model,
                                   error=f"provider returned HTTP {response.status_code}")
            payload = response.json()
            usage = payload.get("usage") or {}
            cost = usage.get("cost")
            if cost is None and usage.get("prompt_tokens") is not None:
                rate_in, rate_out = self._rates_for(model)
                cost = (usage["prompt_tokens"] * rate_in
                        + usage.get("completion_tokens", 0) * rate_out) / 1e6
            if token is not None and cost is not None:
                self._guard.settle(token, float(cost))
            try:
                content = payload["choices"][0]["message"]["content"] or ""
                draft = NoteDraft.model_validate(_json_object(content))
            except ValidationError as exc:
                fields = sorted({".".join(map(str, e["loc"])) for e in exc.errors()})
                logger.warning("desk writer output invalid fields=%s", fields)
                return WriteResult(None, model, cost, error="model output was not valid")
            except (KeyError, IndexError, ValueError) as exc:
                choice = (payload.get("choices") or [{}])[0]
                content = (choice.get("message") or {}).get("content") or ""
                details = usage.get("completion_tokens_details") or {}
                logger.warning(
                    "desk writer output invalid: %s finish_reason=%s chars=%d "
                    "completion_tokens=%s reasoning_tokens=%s max_tokens=%d",
                    type(exc).__name__, choice.get("finish_reason"), len(content),
                    usage.get("completion_tokens"), details.get("reasoning_tokens"),
                    self._max_output,
                )
                return WriteResult(None, model, cost, error="model output was not valid")
            problem = check_draft(draft, rates_read_required=prompt.rates_read_required)
            if problem:
                return WriteResult(None, model, cost, error=problem)
            return WriteResult(draft, model, cost)
        finally:
            if self._client is None:
                await client.aclose()
