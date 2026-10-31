"""CNBC quote adapter.

CNBC's public quote webservice is free and needs no key. It returns one record
per requested vendor symbol with a last price, change, change percent, session
status, and (usually) a day open/high/low and previous close.

Scope notes, verified against live responses:

* futures require an explicit contract month (``NQZ26``, ``YMZ26``), not a
  continuous symbol (``@YM.1`` returns nothing); the contract is resolved in
  :mod:`core.domain.contracts`. WTI uses
  ``CLZ26`` and so on: the continuous ``@CL.1`` kept serving the expired
  November contract's last trade for eight hours after it expired (Oct 20, 2026),
* gold still uses the continuous ``@GC.1`` (an active-month series),
* ``.VIX``/``.VXN`` are the CBOE spot indices, not the futures (``@VX.1`` is the
  futures product and is deliberately not used for ``VIX``),
* ``.DXY`` is the ICE U.S. Dollar Index,
* ``US2Y``/``US10Y`` come back as ``4.688%`` strings and are normalized to the
  canonical percent unit (``4.688``), never fractions or basis points.

No vendor symbol, field name, or payload shape leaves this module.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import httpx

from core.domain.contracts import DEFAULT_FUTURES_ROOTS, active_contract
from core.domain.instruments import Instrument, get_instrument
from core.domain.models import MarketObservation, utc_now
from core.domain.sessions import closed_market_delay
from core.providers.base import (
    MarketDataProvider,
    ProviderHealth,
)

CNBC_QUOTE_URL = (
    "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
    "?symbols={symbols}&requestMethod=itv&noform=1&partnerId=2&fund=1&exthrs=1"
    "&output=json&events=1"
)
NEW_YORK = ZoneInfo("America/New_York")

Clock = Callable[[], datetime]

#: Canonical symbol -> CNBC symbol. ``NQ``/``ES``/``YM``/``WTI`` are generated per
#: active contract at fetch time, so they are handled separately.
SYMBOL_MAP: dict[str, str] = {
    "US2Y": "US2Y",
    "US10Y": "US10Y",
    "DXY": ".DXY",
    "VIX": ".VIX",
    "VXN": ".VXN",
    "GOLD": "@GC.1",
    "NVDA": "NVDA.O",
    "MSFT": "MSFT.O",
    "AAPL": "AAPL.O",
    "AMZN": "AMZN.O",
    "META": "META.O",
    "GOOGL": "GOOGL.O",
    "AVGO": "AVGO.O",
    "TSLA": "TSLA.O",
}

#: Canonical symbol -> the exchange delay (seconds) CNBC serves it with. A
#: listed symbol is labelled delayed, and only counts as stale once it is older
#: than this delay plus the staleness margin. An unlisted symbol is expected in
#: real time, so a lagging quote for it turns stale. A new symbol on this
#: adapter needs an entry here if CNBC serves it delayed. Observed lag is never
#: turned into an allowance automatically.
EXPECTED_DELAY_SECONDS: dict[str, int] = {
    "NQ": 600,
    "ES": 600,
    "YM": 600,
    "WTI": 600,
    "GOLD": 600,
    "VXN": 900,
}

SESSION_STATUS = {"REG_MKT", "PRE_MKT", "POST_MKT", "EXT_MKT", "OPEN"}


def _number(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text.upper() in ("UNCH", "N/A", "--", "-"):
        return None
    if text.endswith("%"):
        text = text[:-1]
    try:
        number = float(text)
    except ValueError:
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _stamp(record: dict, now: datetime) -> tuple[datetime, bool]:
    """Return ``(timestamp, exchange_time_known)``.

    When the provider reports only a date, it is reporting the last session's
    value. The timestamp is anchored to the regular cash close so age is
    meaningful and never counted from midnight; ``False`` marks that the exact
    exchange timestamp is unavailable rather than known.
    """
    raw = record.get("last_time")
    if isinstance(raw, str) and raw:
        text = raw.replace(" ", "T")
        if "T" in text:
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                parsed = None
            if parsed is not None:
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=NEW_YORK)
                return parsed.astimezone(UTC), True
        else:
            try:
                day = date.fromisoformat(text)
            except ValueError:
                day = None
            if day is not None:
                close = datetime(day.year, day.month, day.day, 16, 0, tzinfo=NEW_YORK)
                return close.astimezone(UTC), False
    return now, False


class CNBCMarketDataProvider(MarketDataProvider):
    """Free, no-key quote provider for futures, indices, yields and equities."""

    def __init__(
        self,
        *,
        symbols: Sequence[str] | None = None,
        update_interval: float = 5.0,
        clock: Clock = utc_now,
        client: httpx.AsyncClient | None = None,
        roots: dict[str, str] | None = None,
        futures_roll_days: int = 8,
        live_lag_seconds: float = 60.0,
    ) -> None:
        if update_interval <= 0:
            raise ValueError("update_interval must be positive")
        if futures_roll_days < 0:
            raise ValueError("futures_roll_days must not be negative")
        if live_lag_seconds < 0:
            raise ValueError("live_lag_seconds must not be negative")
        self._update_interval = update_interval
        self._clock = clock
        self._client = client
        self._owned = client is None
        self._roots = dict(roots or DEFAULT_FUTURES_ROOTS)
        self._roll_days = futures_roll_days
        self._live_lag_seconds = live_lag_seconds
        selected = tuple(symbols) if symbols is not None else tuple(SYMBOL_MAP) + tuple(self._roots)
        self._instruments: tuple[Instrument, ...] = tuple(
            instrument for symbol in selected if (instrument := get_instrument(symbol)) is not None
        )
        if not self._instruments:
            raise ValueError("CNBC provider has no supported instruments")
        self._closed = False
        self._last_success: datetime | None = None
        self._last_error: str | None = None
        self._failures = 0
        self._reconnects = 0
        self._delayed_symbols: frozenset[str] = frozenset()

    @property
    def name(self) -> str:
        return "cnbc"

    @property
    def instruments(self) -> Sequence[Instrument]:
        return self._instruments

    def _vendor_symbols(self) -> dict[str, str]:
        today = self._clock().astimezone(NEW_YORK).date()
        mapping: dict[str, str] = {}
        for symbol in (instrument.symbol for instrument in self._instruments):
            if symbol in SYMBOL_MAP:
                mapping[symbol] = SYMBOL_MAP[symbol]
            elif symbol in self._roots:
                mapping[symbol] = active_contract(
                    self._roots[symbol], today, roll_days_before_expiry=self._roll_days
                ).provider_symbol()
        return mapping

    async def _request(self, symbols: dict[str, str]) -> dict:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=15.0,
                limits=httpx.Limits(max_connections=2),
                headers={"User-Agent": "Omarkets/0.3 (+local workstation)"},
            )
        reverse = {vendor: canonical for canonical, vendor in symbols.items()}
        url = CNBC_QUOTE_URL.format(symbols="%7C".join(symbols.values()))
        for attempt in range(2):
            try:
                response = await self._client.get(url, timeout=15.0)
            except httpx.TransportError:
                if attempt:
                    raise
                await asyncio.sleep(1.0)
                continue
            if response.status_code == 429:
                raise _RateLimited("CNBC rate limited the request")
            if response.status_code != 200:
                raise _ProviderFailure(f"CNBC returned HTTP {response.status_code}")
            payload = response.json()
            records = payload.get("FormattedQuoteResult", {}).get("FormattedQuote")
            if not isinstance(records, list):
                raise _ProviderFailure("CNBC payload missing quote list")
            result: dict = {"reverse": reverse, "records": records}
            return result
        raise _ProviderFailure("CNBC request failed")

    def _normalize(self, symbol: str, record: dict, now: datetime) -> MarketObservation | None:
        instrument = get_instrument(symbol)
        if instrument is None:
            return None
        value = _number(record.get("last"))
        if value is None:
            return None
        if instrument.quote_type.value == "yield" and value > 25:
            return None
        if instrument.asset_class.value == "volatility_index" and value <= 0:
            return None
        previous = _number(record.get("previous_day_closing"))
        day_open = _number(record.get("open"))
        high = _number(record.get("high"))
        low = _number(record.get("low"))
        # CNBC's volume/open/high/low are for the *futures* session (Globex),
        # not US RTH. Volume is cumulative over that session and is carried
        # through unmodified; downstream code differences consecutive readings
        # within a session/contract and never treats it as an interval value.
        volume = _number(record.get("volume"))
        session = str(record.get("curmktstatus") or "")
        status, exchange_time_known = _stamp(record, now)
        # A vendor can serve values whose exchange timestamp is minutes behind
        # receipt. That is a delayed feed and must never be labelled live.
        lag = max(0.0, (now - status).total_seconds())
        in_session = session in SESSION_STATUS
        expected_delay = EXPECTED_DELAY_SECONDS.get(symbol, 0)
        if not exchange_time_known:
            feed = "stale"
        elif in_session and (expected_delay or lag > self._live_lag_seconds):
            feed = "delayed"
        elif in_session:
            feed = "live"
        else:
            feed = "stale"
        metadata: dict = {
            "provider_symbol": record.get("symbol"),
            "session_status": session,
            "exchange": record.get("exchange"),
            "exchange_time_known": exchange_time_known,
            "feed_status": feed,
            "delayed": feed == "delayed",
            "expected_delay_seconds": expected_delay,
        }
        if not exchange_time_known:
            metadata["stale_reason"] = (
                "provider reported only a session date; exact exchange time unavailable"
            )
        elif feed == "delayed":
            metadata["stale_reason"] = (
                f"exchange timestamp lagged receipt by {lag:.0f}s; feed is delayed"
            )
        if previous is not None:
            metadata["previous_close"] = previous
            metadata["baseline_period"] = status.astimezone(NEW_YORK).date().isoformat()
        if day_open is not None:
            metadata["open"] = day_open
        if high is not None:
            metadata["high"] = high
        if low is not None:
            metadata["low"] = low
        if volume is not None:
            metadata["volume"] = int(volume)
        if symbol in self._roots:
            contract = active_contract(
                self._roots[symbol],
                now.astimezone(NEW_YORK).date(),
                roll_days_before_expiry=self._roll_days,
            )
            metadata["contract_code"] = contract.code
            metadata["contract_expiry"] = contract.expiry.isoformat()
        return MarketObservation(
            symbol=symbol,
            timestamp=status,
            value=value,
            unit=instrument.unit,
            source=self.name,
            received_at=self._clock(),
            metadata=metadata,
        )

    async def _collect(self) -> list[MarketObservation]:
        symbols = self._vendor_symbols()
        if not symbols:
            return []
        data = await self._request(symbols)
        now = self._clock()
        observations = []
        for record in data["records"]:
            canonical = data["reverse"].get(record.get("symbol"))
            if canonical is None:
                continue
            observation = self._normalize(canonical, record, now)
            if observation is not None:
                observations.append(observation)
        return observations

    async def start(self) -> None:
        self._closed = False

    async def fetch_snapshot(self) -> list[MarketObservation]:
        if self._closed:
            raise RuntimeError("provider is closed")
        try:
            observations = await self._collect()
        except _RateLimited as exc:
            self._register_failure(str(exc))
            raise
        except Exception as exc:
            self._register_failure(type(exc).__name__)
            raise
        if observations:
            self._last_success = self._clock()
            self._last_error = None
            self._failures = 0
        return observations

    def _register_failure(self, detail: str) -> None:
        self._failures += 1
        self._last_error = detail

    async def stream_updates(self) -> AsyncIterator[list[MarketObservation]]:
        """Poll on a fixed interval with bounded backoff on failure.

        CNBC has no public websocket, so "stream" here is a bounded poll. The
        iterator never busy-loops and stops promptly on cancellation.
        """
        backoff = self._update_interval
        try:
            while not self._closed:
                # Recheck wall-clock session state promptly after laptop resume.
                # A long monotonic sleep can remain pending across suspension.
                quiet_delay = closed_market_delay(self._clock(), maximum_seconds=30.0)
                if quiet_delay > 0:
                    await asyncio.sleep(quiet_delay)
                    continue
                try:
                    observations = await self._collect()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._register_failure(type(exc).__name__)
                    self._reconnects += 1
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60.0)
                    continue
                backoff = self._update_interval
                if observations:
                    self._last_success = self._clock()
                    self._last_error = None
                    self._failures = 0
                    yield observations
                await asyncio.sleep(self._update_interval)
        except asyncio.CancelledError:
            raise

    async def health(self) -> ProviderHealth:
        healthy = not self._closed and self._last_error is None
        detail = "stopped" if self._closed else (self._last_error or "CNBC quote poll")
        return ProviderHealth(
            name=self.name,
            healthy=healthy,
            checked_at=self._clock(),
            detail=detail,
        )

    def diagnostics(self) -> dict:
        return {
            "connection": "open" if not self._closed else "closed",
            "last_success": self._last_success.isoformat() if self._last_success else None,
            "last_error": self._last_error,
            "consecutive_failures": self._failures,
            "reconnect_count": self._reconnects,
            "delayed_symbols": sorted(self._delayed_symbols),
        }

    async def aclose(self) -> None:
        self._closed = True
        if self._owned and self._client is not None:
            await self._client.aclose()
            self._client = None


class _RateLimited(RuntimeError):
    pass


class _ProviderFailure(RuntimeError):
    pass


__all__ = ["CNBCMarketDataProvider", "EXPECTED_DELAY_SECONDS", "SYMBOL_MAP"]
