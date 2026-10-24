"""US Treasury note, bond and TIPS auctions from TreasuryDirect's public web service.

Endpoints (public JSON, no key):

* ``/TA_WS/securities/upcoming``  — announced and tentative auctions.
* ``/TA_WS/securities/auctioned?type=Note|Bond|TIPS&days=N`` — results.

Payloads are untrusted: every field is validated here and anything malformed
is dropped. Only the coupon auctions that matter most for yields are kept —
2/5/7/10-year notes, 20/30-year bonds and 5/10/30-year TIPS — including
reopenings, which are matched by their original term. Nothing
TreasuryDirect-specific leaves this module.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

NEW_YORK = ZoneInfo("America/New_York")
BASE = "https://www.treasurydirect.gov/TA_WS/securities"
NOMINAL_YEARS = frozenset({2, 5, 7, 10, 20, 30})
TIPS_YEARS = frozenset({5, 10, 30})
#: Coupon auctions close for competitive bids at 1:00 PM ET.
USUAL_CLOSE = time(13, 0)

Kind = Literal["note", "bond", "tips"]


class AuctionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    high_yield: float = Field(gt=-5, lt=30)
    bid_to_cover: float = Field(gt=0, lt=20)
    #: Share of the competitive award taken by primary dealers (percent).
    dealer_share_pct: float | None = Field(default=None, ge=0, le=100)


class Auction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cusip: str
    kind: Kind
    #: Original term in years (a reopened 10-year is still a 10-year).
    years: int
    reopening: bool
    auction_date: date
    #: Competitive close time, when TreasuryDirect has announced it.
    close_time: time | None
    offering_usd: float | None = None
    result: AuctionResult | None = None

    @property
    def label(self) -> str:
        name = "TIPS" if self.kind == "tips" else self.kind
        return f"{self.years}-year {name}" + (" (reopening)" if self.reopening else "")

    @property
    def series(self) -> str:
        """Auctions compared with each other: same kind and original term."""
        return f"{'tips' if self.kind == 'tips' else 'nominal'}:{self.years}"

    @property
    def at(self) -> datetime:
        """Results time in UTC: the announced close, else the usual 1:00 PM ET."""
        clock = self.close_time or USUAL_CLOSE
        return datetime.combine(self.auction_date, clock, tzinfo=NEW_YORK).astimezone(UTC)


_YEARS = re.compile(r"^(\d{1,2})-Year$")
_TIME = re.compile(r"^(\d{1,2}):(\d{2}) (AM|PM)$")
_CUSIP = re.compile(r"^[0-9A-Z]{9}$")


def _number(value: object) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _time(value: object) -> time | None:
    match = _TIME.match(value.strip()) if isinstance(value, str) else None
    if not match:
        return None
    hour, minute = int(match.group(1)) % 12, int(match.group(2))
    if match.group(3) == "PM":
        hour += 12
    return time(hour, minute) if minute < 60 else None


def _result(row: dict) -> AuctionResult | None:
    high, cover = _number(row.get("highYield")), _number(row.get("bidToCoverRatio"))
    if high is None or cover is None or cover <= 0:
        return None
    awarded = [_number(row.get(f"{group}CompetitiveParAwarded"))
               for group in ("primaryDealers", "directBidders", "indirectBidders")]
    dealer = None
    if all(v is not None and v >= 0 for v in awarded) and sum(awarded) > 0:
        dealer = round(100 * awarded[0] / sum(awarded), 1)
    try:
        return AuctionResult(high_yield=high, bid_to_cover=cover, dealer_share_pct=dealer)
    except ValueError:
        return None


def parse_row(row: object) -> Auction | None:
    """One TreasuryDirect security, or ``None`` if it is not a tracked, valid auction."""
    if not isinstance(row, dict):
        return None
    if row.get("floatingRate") == "Yes" or row.get("cashManagementBill") == "Yes":
        return None
    tips = row.get("inflationProtected") == "Yes"
    security = row.get("securityType")
    if security not in ("Note", "Bond"):
        return None
    match = _YEARS.match(str(row.get("originalSecurityTerm") or row.get("securityTerm") or ""))
    cusip = str(row.get("cusip") or "")
    auction_date = _date(row.get("competitiveAuctionCloseDate") or row.get("auctionDate"))
    if not match or not _CUSIP.match(cusip) or auction_date is None:
        return None
    years = int(match.group(1))
    if years not in (TIPS_YEARS if tips else NOMINAL_YEARS):
        return None
    offering = _number(row.get("offeringAmount"))
    return Auction(
        cusip=cusip,
        kind="tips" if tips else ("note" if security == "Note" else "bond"),
        years=years,
        reopening=row.get("reopening") == "Yes",
        auction_date=auction_date,
        close_time=_time(row.get("competitiveAuctionCloseTime")),
        offering_usd=offering if offering and offering > 0 else None,
        result=_result(row),
    )


def parse(payload: object) -> list[Auction]:
    if not isinstance(payload, list):
        return []
    auctions = [a for a in (parse_row(row) for row in payload) if a is not None]
    # One row per (cusip, date); a later row with results wins.
    unique: dict[tuple[str, date], Auction] = {}
    for auction in auctions:
        key = (auction.cusip, auction.auction_date)
        if key not in unique or (auction.result and not unique[key].result):
            unique[key] = auction
    return sorted(unique.values(), key=lambda a: (a.auction_date, a.years))


class TreasuryAuctions:
    """Caches the schedule and results; checks results often only on auction afternoons."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        ttl: timedelta = timedelta(hours=6),
        results_poll: timedelta = timedelta(minutes=2),
        history_days: int = 400,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._ttl = ttl
        self._results_poll = results_poll
        self._history_days = history_days
        self._clock = clock
        self._upcoming: tuple[datetime, list[Auction]] | None = None
        self._results: tuple[datetime, list[Auction]] | None = None
        self.last_error: str | None = None
        # One fetch at a time, so concurrent readers share a single request.
        self._lock = asyncio.Lock()

    async def _get(self, path: str, params: dict[str, str]) -> object:
        client = self._client or httpx.AsyncClient(timeout=20.0)
        try:
            response = await client.get(
                f"{BASE}/{path}", params={"format": "json", **params},
                headers={"User-Agent": "Omarkets (local market desk)",
                         "Accept": "application/json"},
            )
            response.raise_for_status()
            return response.json()
        finally:
            if self._client is None:
                await client.aclose()

    async def upcoming(self) -> list[Auction]:
        async with self._lock:
            return await self._upcoming_locked()

    async def _upcoming_locked(self) -> list[Auction]:
        now = self._clock()
        if self._upcoming and now - self._upcoming[0] < self._ttl:
            return self._upcoming[1]
        try:
            rows = parse(await self._get("upcoming", {}))
        except Exception as exc:  # noqa: BLE001 - provider isolation
            self.last_error = f"Treasury auction schedule unavailable ({type(exc).__name__})"
            logger.warning("treasury upcoming fetch failed: %s", type(exc).__name__)
            return self._upcoming[1] if self._upcoming else []
        self.last_error = None
        self._upcoming = (now, rows)
        return rows

    def _awaiting_results(self, now: datetime) -> bool:
        """True on an auction afternoon until that auction's results are in."""
        schedule = self._upcoming[1] if self._upcoming else []
        known = {a.cusip for a in (self._results[1] if self._results else []) if a.result}
        return any(a.at <= now <= a.at + timedelta(hours=3) and a.cusip not in known
                   for a in schedule)

    async def results(self) -> list[Auction]:
        """Auctions with results over roughly the last year, oldest first."""
        async with self._lock:
            return await self._results_locked()

    async def _results_locked(self) -> list[Auction]:
        now = self._clock()
        if self._results is not None:
            age = now - self._results[0]
            fresh = self._results_poll if self._awaiting_results(now) else self._ttl
            if age < fresh:
                return self._results[1]
        rows: list[object] = []
        try:
            for kind in ("Note", "Bond", "TIPS"):
                payload = await self._get("auctioned",
                                          {"type": kind, "days": str(self._history_days)})
                if isinstance(payload, list):
                    rows.extend(payload)
        except Exception as exc:  # noqa: BLE001 - provider isolation
            self.last_error = f"Treasury auction results unavailable ({type(exc).__name__})"
            logger.warning("treasury results fetch failed: %s", type(exc).__name__)
            return self._results[1] if self._results else []
        self.last_error = None
        auctions = [a for a in parse(rows) if a.result is not None]
        self._results = (now, auctions)
        return auctions


__all__ = ["Auction", "AuctionResult", "TreasuryAuctions", "parse", "parse_row"]
