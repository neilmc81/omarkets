"""Treasury auctions on the desk: the strong/weak rule, Coming up, and notes."""

from datetime import UTC, date, datetime, time, timedelta

from core.desk import auctions as A
from core.desk.models import NoteKind
from core.providers.treasury_auctions import Auction, AuctionResult
from tests.desk.test_service import FakeWriter, make


def auction(day, cover, *, years=10, kind="note", dealer=10.0, cusip="91282CRF0",
            close=time(13, 0), result=True):
    return Auction(cusip=cusip, kind=kind, years=years, reopening=False, auction_date=day,
                   close_time=close, offering_usd=42e9,
                   result=AuctionResult(high_yield=4.5, bid_to_cover=cover,
                                        dealer_share_pct=dealer) if result else None)


def history(covers, *, years=10, kind="note"):
    return [auction(date(2026, 3, 10) + timedelta(days=30 * i), c, years=years, kind=kind)
            for i, c in enumerate(covers)]


TODAY = date(2026, 10, 21)


def test_strong_weak_and_ordinary_against_the_last_six():
    past = history([2.0, 2.0, 2.5, 2.5, 2.5, 2.5, 2.5, 2.5])  # last six average 2.50
    assert A.judge(auction(TODAY, 2.63), past).verdict == "strong"  # +5.2%
    assert A.judge(auction(TODAY, 2.37), past).verdict == "weak"  # -5.2%
    read = A.judge(auction(TODAY, 2.55), past)
    assert read.verdict == "ordinary" and read.average_bid_to_cover == 2.5
    assert read.history_count == 6
    assert read.text == ("10-year note: ordinary demand — bid-to-cover 2.55 versus 2.50 on "
                         "average at the last 6; sold at 4.500%.")


def test_series_are_separate_and_only_earlier_auctions_count():
    tips = history([1.0] * 6, kind="tips")
    later = [auction(TODAY + timedelta(days=30), 1.0)]
    two_year = history([1.0] * 6, years=2)
    read = A.judge(auction(TODAY, 2.6), tips + later + two_year)
    assert read.verdict == "not_enough_history" and read.history_count == 0
    assert "not enough earlier auctions" in read.text


def test_no_result_no_read():
    assert A.judge(auction(TODAY, 2.5, result=False), history([2.5] * 6)) is None


def test_upcoming_items_say_when_the_time_is_not_announced():
    now = datetime(2026, 10, 20, 12, tzinfo=UTC)
    items = A.upcoming_items(
        [auction(TODAY, 2.5, result=False),
         auction(date(2026, 10, 26), 2.5, years=2, close=None, result=False),
         auction(date(2026, 12, 1), 2.5, result=False)],  # outside the window
        now=now, until=now + timedelta(days=7))
    assert [i.title for i in items] == ["Treasury auction: 10-year note",
                                        "Treasury auction: 2-year note"]
    assert items[0].importance == "high" and items[1].importance == "medium"
    assert items[0].when_text == "results at 1:00 PM ET"
    assert items[1].when_text == "time not announced yet; usually 1:00 PM ET"
    assert items[0].detail == "$42 billion on offer" and items[0].kind == "auction"


class FakeAuctions:
    def __init__(self, results, upcoming=()):
        self._results, self._upcoming = list(results), list(upcoming)

    async def results(self):
        return self._results

    async def upcoming(self):
        return self._upcoming


async def test_a_weak_auction_gets_one_note_after_the_reaction_window():
    results = history([2.5] * 6) + [auction(TODAY, 2.2)]
    at = datetime(2026, 10, 21, 17, 0, tzinfo=UTC)  # 1:00 PM EDT
    writer = FakeWriter()
    service, clock, notes = make(at + timedelta(minutes=25), writer=writer)
    service.auctions = FakeAuctions(results)
    assert not [n for n in await service.tick() if n.kind is NoteKind.AUCTION]
    clock["now"] = at + timedelta(minutes=27)
    produced = [n for n in await service.tick() if n.kind is NoteKind.AUCTION]
    assert [n.id for n in produced] == ["auction:2026-10-21:91282CRF0"]
    facts = next(f for kind, f in writer.calls if kind is NoteKind.AUCTION)
    assert facts["auction"]["demand"] == "weak"
    assert facts["auction"]["average_bid_to_cover_recent"] == "2.50"
    clock["now"] = at + timedelta(minutes=40)
    assert not [n for n in await service.tick() if n.kind is NoteKind.AUCTION]


async def test_an_ordinary_auction_gets_no_note_but_shows_in_coming_up():
    at = datetime(2026, 10, 21, 17, 0, tzinfo=UTC)
    service, clock, _ = make(at + timedelta(minutes=30))
    service.auctions = FakeAuctions(history([2.5] * 6) + [auction(TODAY, 2.5)],
                                    upcoming=[auction(date(2026, 10, 22), 0, years=5,
                                                      kind="tips", result=False)])
    assert not [n for n in await service.tick() if n.kind is NoteKind.AUCTION]
    upcoming = await service.upcoming(clock["now"], days=7)
    assert [u.title for u in upcoming] == ["Treasury auction: 5-year TIPS"]
