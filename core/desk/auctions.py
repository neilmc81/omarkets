"""Treasury auctions on the desk: what is coming up, and whether a result was strong or weak.

The rule is deterministic and stated in docs/desk.md. A free, reliable
when-issued yield is not available, so the auction "tail" cannot be measured.
Instead each auction's bid-to-cover ratio — dollars bid per dollar sold — is
compared with the average of the previous :data:`HISTORY` auctions of the same
series (same original term; TIPS separately):

* **strong** when it is at least :data:`MARGIN` (5%) above that average,
* **weak** when it is at least 5% below,
* otherwise ordinary — no note is written.

With fewer than :data:`MIN_HISTORY` earlier auctions there is no verdict.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict

from core.desk import explainers as X
from core.desk.models import UpcomingItem
from core.providers.treasury_auctions import Auction

HISTORY = 6
MIN_HISTORY = 4
MARGIN = 0.05
#: The two auctions that move yields most get "high" importance on the desk.
HIGH_IMPORTANCE = frozenset({"nominal:10", "nominal:30"})

AuctionVerdict = Literal["strong", "weak", "ordinary", "not_enough_history"]


class AuctionRead(BaseModel):
    """One auction result judged against its own series."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: AuctionVerdict
    high_yield: float
    bid_to_cover: float
    average_bid_to_cover: float | None
    history_count: int
    dealer_share_pct: float | None
    average_dealer_share_pct: float | None
    text: str


def _mean(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def judge(auction: Auction, history: Sequence[Auction]) -> AuctionRead | None:
    """Strong, weak or ordinary versus the previous auctions in the same series."""
    if auction.result is None:
        return None
    earlier = sorted(
        (a for a in history if a.series == auction.series and a.result is not None
         and a.auction_date < auction.auction_date),
        key=lambda a: a.auction_date, reverse=True,
    )[:HISTORY]
    covers = [a.result.bid_to_cover for a in earlier]
    dealers = [a.result.dealer_share_pct for a in earlier if a.result.dealer_share_pct is not None]
    average = _mean(covers)
    result = auction.result
    if len(earlier) < MIN_HISTORY or average is None:
        verdict: AuctionVerdict = "not_enough_history"
        text = (f"{auction.label}: sold at {result.high_yield:.3f}%, bid-to-cover "
                f"{result.bid_to_cover:.2f}; not enough earlier auctions to compare.")
    else:
        ratio = result.bid_to_cover / average
        verdict = ("strong" if ratio >= 1 + MARGIN else "weak" if ratio <= 1 - MARGIN
                   else "ordinary")
        words = {"strong": "strong demand", "weak": "weak demand", "ordinary": "ordinary demand"}
        text = (f"{auction.label}: {words[verdict]} — bid-to-cover {result.bid_to_cover:.2f} "
                f"versus {average:.2f} on average at the last {len(earlier)}; sold at "
                f"{result.high_yield:.3f}%.")
    return AuctionRead(
        verdict=verdict, high_yield=result.high_yield, bid_to_cover=result.bid_to_cover,
        average_bid_to_cover=average, history_count=len(earlier),
        dealer_share_pct=result.dealer_share_pct, average_dealer_share_pct=_mean(dealers),
        text=text,
    )


EXPLAINER = X.topic("treasury_auction").text


def upcoming_items(auctions: Sequence[Auction], *, now: datetime,
                   until: datetime) -> list[UpcomingItem]:
    items: list[UpcomingItem] = []
    for auction in auctions:
        if not (now - timedelta(hours=1) <= auction.at < until):
            continue
        when = ("time not announced yet; usually 1:00 PM ET" if auction.close_time is None
                else f"results at {auction.close_time:%-I:%M %p} ET")
        detail = (f"${auction.offering_usd / 1e9:.0f} billion on offer"
                  if auction.offering_usd else "Size not announced yet")
        items.append(UpcomingItem(
            kind="auction", title=f"Treasury auction: {auction.label}", at=auction.at,
            when_text=when,
            importance="high" if auction.series in HIGH_IMPORTANCE else "medium",
            detail=detail, explainer=EXPLAINER,
        ))
    return items


__all__ = ["AuctionRead", "judge", "upcoming_items"]
