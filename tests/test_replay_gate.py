"""Replay gate: NQ/ES state and chart JSON stay identical to the recorded baseline.

The multi-asset plan (``docs/PLAN.md`` §6) requires every phase to leave the
NQ screens unchanged. This test replays one fixed set of CNBC- and
Binance-shaped observations (late September to late November 2026, so it
crosses the US daylight-saving change, Thanksgiving and its early close) and
renders, at eight instants:

* the market state (every instrument) and the desk quote strip,
* the bars endpoint for NQ, ES and the chart overlays (ES, US2Y, DXY) in every
  range, plus the session timeline, key levels, risk tone and sparklines.

The SHA-256 of each canonical JSON payload must match
``tests/fixtures/replay/phase0_golden.json``, which was recorded on ``main`` at
65f4212, before Phase 0 changed any session or staleness code. BTC bars are left
out on purpose: Phase 0 moves BTC to its own 24×7 calendar.

To re-record (only when a change to these screens is intended and agreed):
``OMARKETS_REPLAY_RECORD=<dir> pytest tests/test_replay_gate.py`` writes the
digests and the full payloads to ``<dir>``.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from core.api import chart
from core.desk import facts as F
from core.domain.instruments import INSTRUMENTS
from core.domain.models import MarketObservation
from core.services.market_state import StalePolicy, build_market_state
from core.storage.duckdb_repo import DuckDBObservationRepository

GOLDEN = Path(__file__).parent / "fixtures" / "replay" / "phase0_golden.json"
NEW_YORK = ZoneInfo("America/New_York")
START = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
END = datetime(2026, 11, 28, 0, 0, tzinfo=UTC)
STEP = timedelta(minutes=10)

#: Instants rendered, in UTC.
MOMENTS: dict[str, datetime] = {
    "thu_rth": datetime(2026, 10, 22, 15, 0, tzinfo=UTC),        # 11:00 EDT
    "thu_break": datetime(2026, 10, 22, 21, 30, tzinfo=UTC),     # 17:30 EDT
    "sat_closed": datetime(2026, 10, 24, 16, 0, tzinfo=UTC),     # Saturday noon
    "sun_reopen": datetime(2026, 10, 25, 22, 30, tzinfo=UTC),    # Sunday 18:30 EDT
    "mon_after_dst": datetime(2026, 11, 2, 15, 0, tzinfo=UTC),   # 10:00 EST
    "thanksgiving": datetime(2026, 11, 26, 17, 0, tzinfo=UTC),   # holiday noon
    "early_close": datetime(2026, 11, 27, 17, 0, tzinfo=UTC),    # 12:00 EST, half day
    "after_early": datetime(2026, 11, 27, 19, 0, tzinfo=UTC),    # 14:00 EST, shut
}

#: CNBC-routed symbols in the fixture: (base value, tick, declared delay seconds,
#: exchange lag behind receipt in seconds, contract code).
CNBC: dict[str, tuple[float, float, int, int, str | None]] = {
    "NQ": (29_000.0, 0.25, 600, 600, "NQZ26"),
    "ES": (7_600.0, 0.25, 600, 600, "ESZ26"),
    "US2Y": (4.6, 0.001, 0, 5, None),
    "US10Y": (4.9, 0.001, 0, 5, None),
    "DXY": (101.5, 0.001, 0, 5, None),
    "VIX": (18.0, 0.01, 0, 900, None),
    "WTI": (100.0, 0.01, 600, 600, "CLZ26"),
    "GOLD": (4_000.0, 0.1, 600, 600, None),
}

#: Endpoints rendered per instant. BTC bars are deliberately absent (see above).
CHART_REQUESTS: tuple[str, ...] = tuple(
    f"/api/v1/bars/{symbol}?interval={interval}&range={chart_range}"
    for symbol in ("NQ", "ES", "US2Y", "DXY")
    for interval, chart_range in (("5m", "1d"), ("15m", "5d"), ("1h", "5d"), ("1h", "1m"))
) + ("/api/v1/session", "/api/v1/levels/NQ", "/api/v1/levels/ES", "/api/v1/risk-tone",
     "/api/v1/sparklines")


def _cnbc_quoting(moment: datetime) -> bool:
    """When the fixture's CNBC feed sends quotes: Globex hours, written out by hand.

    Deliberately independent of ``core.domain.sessions`` so the fixture does not
    move when the session code does.
    """
    local = moment.astimezone(NEW_YORK)
    weekday, hour = local.weekday(), local.hour
    if local.date() == datetime(2026, 11, 26).date():
        return False  # Thanksgiving
    if local.date() == datetime(2026, 11, 27).date() and (hour, local.minute) >= (13, 15):
        return False  # early close
    if weekday == 5 or (weekday == 4 and hour >= 17) or (weekday == 6 and hour < 18):
        return False  # weekend
    return hour != 17  # daily break


def _value(base: float, tick: float, index: int) -> float:
    raw = base * (1.0 + 0.004 * math.sin(index / 9.0) + 0.002 * math.sin(index / 53.0))
    return round(round(raw / tick) * tick, 6)


def observations() -> list[MarketObservation]:
    """The replay fixture: deterministic, CNBC/Binance-shaped quotes."""
    rows: list[MarketObservation] = []
    index = 0
    moment = START
    while moment < END:
        index += 1
        if _cnbc_quoting(moment):
            for symbol, (base, tick, delay, lag, contract) in CNBC.items():
                stamp = moment - timedelta(seconds=lag)
                value = _value(base, tick, index)
                metadata: dict = {
                    "provider_symbol": contract or symbol,
                    "session_status": "REG_MKT",
                    "exchange_time_known": True,
                    "feed_status": "delayed" if (delay or lag > 60) else "live",
                    "delayed": bool(delay or lag > 60),
                    "expected_delay_seconds": delay,
                    "previous_close": _value(base, tick, index - 144),
                    "baseline_period": stamp.astimezone(NEW_YORK).date().isoformat(),
                }
                if contract:
                    metadata["contract_code"] = contract
                rows.append(MarketObservation(
                    symbol=symbol, timestamp=stamp, value=value,
                    unit=INSTRUMENTS[symbol].unit, source="cnbc", received_at=moment,
                    metadata=metadata))
        rows.append(MarketObservation(
            symbol="BTC", timestamp=moment, value=_value(80_000.0, 0.01, index), unit="USD",
            source="binance", received_at=moment,
            metadata={"provider_symbol": "BTCUSDT", "session_status": "CRYPTO",
                      "feed_status": "live",
                      "previous_close": _value(80_000.0, 0.01, index - 144)}))
        moment += STEP
    return rows


def latest(rows: list[MarketObservation], now: datetime) -> dict[str, MarketObservation]:
    found: dict[str, MarketObservation] = {}
    for row in rows:
        if row.received_at <= now:
            found[row.symbol] = row
    return found


class ReplayService:
    """The two service methods the chart API reads, over the fixture."""

    def __init__(self, repo, rows: list[MarketObservation]) -> None:
        self.repo = repo
        self.rows = rows
        self.now = START

    def state(self):
        return build_market_state(latest(self.rows, self.now), policy=StalePolicy(),
                                  now=self.now, provider="composite")

    def current_state(self, now=None):
        return self.state()

    def ohlc(self, symbol, *, start, end, bucket_seconds, within=()):
        # The fixture replays history: nothing received after "now" is visible.
        end = min(end, self.now + timedelta(microseconds=1))
        return self.repo.ohlc(symbol, start=start, end=end, bucket_seconds=bucket_seconds,
                              within=within)


def _canonical(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def render(repo) -> dict[str, object]:
    """Every gate payload, keyed ``<moment> <what>``."""
    rows = observations()
    repo.store(rows)
    service = ReplayService(repo, rows)
    app = FastAPI()
    app.include_router(chart.router)
    app.state.service = service
    app.state.desk = None
    payloads: dict[str, object] = {}
    with TestClient(app) as client:
        for name, now in MOMENTS.items():
            service.now = now
            state = service.state()
            payloads[f"{name} state"] = state.model_dump(mode="json")
            payloads[f"{name} quote_strip"] = [
                quote.model_dump(mode="json") for quote in F.quote_strip(state)]
            for path in CHART_REQUESTS:
                response = client.get(path)
                payloads[f"{name} {path}"] = {"status": response.status_code,
                                              "body": response.json()}
    return payloads


@pytest.fixture(scope="module")
def payloads() -> dict[str, object]:
    repository = DuckDBObservationRepository(":memory:")
    repository.initialize()
    try:
        return render(repository)
    finally:
        repository.close()


def test_nq_es_state_and_chart_match_the_recorded_baseline(payloads, tmp_path):
    digests = {key: hashlib.sha256(_canonical(value).encode()).hexdigest()
               for key, value in payloads.items()}
    record = os.environ.get("OMARKETS_REPLAY_RECORD")
    if record:
        out = Path(record)
        out.mkdir(parents=True, exist_ok=True)
        (out / "phase0_golden.json").write_text(json.dumps(digests, indent=1, sort_keys=True)
                                                + "\n")
        (out / "phase0_payloads.json").write_text(json.dumps(payloads, sort_keys=True))
        pytest.skip(f"recorded {len(digests)} replay digests to {out}")
    golden = json.loads(GOLDEN.read_text())
    assert sorted(digests) == sorted(golden)
    changed = sorted(key for key in golden if digests[key] != golden[key])
    if changed:
        (tmp_path / "actual.json").write_text(json.dumps(
            {key: payloads[key] for key in changed}, indent=1, sort_keys=True))
    assert not changed, f"payloads differ from the baseline (see {tmp_path}/actual.json)"


def test_the_fixture_exercises_the_cases_the_gate_is_about(payloads):
    nq = {name: payloads[f"{name} state"]["instruments"]["NQ"] for name in MOMENTS}
    assert nq["thu_rth"]["status"] == "live" and nq["thu_rth"]["feed_status"] == "delayed"
    assert nq["sat_closed"]["market_closed"] and nq["thu_break"]["market_closed"]
    assert nq["thanksgiving"]["market_closed"] and nq["after_early"]["market_closed"]
    assert not nq["sun_reopen"]["market_closed"]
    bars = payloads["mon_after_dst /api/v1/bars/NQ?interval=1h&range=5d"]
    assert bars["status"] == 200 and len(bars["body"]["sessions"]) == 5
    assert sum(1 for bar in bars["body"]["bars"] if bar["count"]) > 50
    month = payloads["thu_rth /api/v1/bars/ES?interval=1h&range=1m"]["body"]
    assert len(month["sessions"]) >= 20
