"""Phase 1 slice 4: complete writer inputs (no prompt change).

Per-quote status, time, delay and baseline; NQ vs ES and megacap
participation; events in a move's window; ranked headlines with publication
and received times kept apart; each instrument's own fear gauge.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from core.desk import facts as F
from core.desk.identity import Instrument
from core.desk.models import NoteKind
from core.domain.instruments import AssetClass, QuoteType
from core.domain.models import DataStatus, FeedStatus, InstrumentState
from tests.desk.test_normal_move import size
from tests.desk.test_service import RELEASE, cpi_rows, make

NOW = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)  # Tue 11:00 ET


def inst(symbol, value=100.0, *, status=DataStatus.LIVE, feed=FeedStatus.DELAYED,
         change_pct=0.5, change=0.5, at=NOW - timedelta(minutes=10), delay=600.0,
         asset=AssetClass.EQUITY_INDEX_FUTURE, quote=QuoteType.PRICE, source="cnbc",
         baseline="2026-10-20", closed=False):
    return InstrumentState(
        symbol=symbol, display_name=symbol, asset_class=asset, quote_type=quote, unit="pts",
        value=value, change=change, change_pct=change_pct, timestamp=at,
        stale_after_seconds=120, status=status, source=source, feed_status=feed,
        expected_delay_seconds=delay, baseline_period=baseline, market_closed=closed)


def state(*items, now=NOW):
    return SimpleNamespace(generated_at=now, instruments={i.symbol: i for i in items})


# --------------------------------------------------------------------------- quotes


def test_quote_rows_keep_live_stale_closed_and_unavailable_apart():
    rows = {r["market"]: r for r in F.market_rows(state(
        inst("NQ"),
        inst("ES", status=DataStatus.STALE, feed=FeedStatus.STALE),
        inst("DXY", status=DataStatus.STALE, closed=True, asset=AssetClass.FX_INDEX,
             delay=0, feed=FeedStatus.STALE),
        inst("US10Y", value=None, status=DataStatus.UNAVAILABLE, change=None,
             change_pct=None, at=None, feed=FeedStatus.UNAVAILABLE, delay=0,
             asset=AssetClass.TREASURY_YIELD, quote=QuoteType.YIELD),
    ))}
    nq = rows["Nasdaq 100 futures"]
    assert nq == {"market": "Nasdaq 100 futures", "level": "100.00", "change_today": "+0.50%",
                  "delayed": True, "stale": False, "status": "live",
                  "as_of": "Tue Oct 20, 10:50 ET", "expected_delay": "about 10 minutes",
                  "change_from": "the previous session's settle"}
    assert rows["S&P 500 futures"]["status"] == "stale"
    assert rows["S&P 500 futures"]["stale"] is True
    assert rows["US dollar"]["status"] == "closed"
    assert rows["US dollar"]["stale"] is False
    assert rows["US dollar"]["change_from"] == "the previous session's close"
    assert "expected_delay" not in rows["US dollar"]  # undeclared, not "none"
    ten = rows["10-year yield"]
    # Unavailable is no longer reported as stale, and carries no time or baseline.
    assert ten["status"] == "unavailable" and ten["stale"] is False
    assert ten["level"] == "—" and "as_of" not in ten and "change_from" not in ten


def test_quote_baselines_and_declared_delays():
    rows = F.market_rows(state(
        inst("NQ", change=None, change_pct=None),
        inst("BTC", source="binance", feed=FeedStatus.LIVE, delay=0,
             asset=AssetClass.CRYPTO),
    ), symbols=("NQ", "BTC"))
    assert rows[0]["change_from"] == "not known (no baseline from the feed)"
    assert rows[0]["change_today"] is None
    assert rows[1]["change_from"] == "the price 24 hours earlier (rolling)"
    assert rows[1]["expected_delay"] == "none"


# --------------------------------------------------------------------------- NQ vs ES


def test_nq_vs_es_compares_live_compatible_quotes():
    out = F.nq_vs_es(state(inst("NQ", change_pct=0.62), inst("ES", change_pct=0.31)))
    assert out == {"comparable": True, "nq_change_today": "+0.62%",
                   "es_change_today": "+0.31%", "nq_minus_es": "+0.31 percentage points",
                   "reading": "NQ did better than ES", "as_of": "Tue Oct 20, 10:50 ET"}
    aligned = F.nq_vs_es(state(inst("NQ", change_pct=0.30), inst("ES", change_pct=0.25)))
    assert aligned["reading"].startswith("NQ and ES moved together")


def test_nq_vs_es_says_why_it_cannot_compare():
    assert F.nq_vs_es(state(inst("ES"))) == {"comparable": False, "why_not": "no NQ quote"}
    stale = F.nq_vs_es(state(inst("NQ"), inst("ES", status=DataStatus.STALE)))
    assert stale == {"comparable": False, "why_not": "the ES quote is not current"}
    skewed = F.nq_vs_es(state(inst("NQ"), inst("ES", at=NOW - timedelta(minutes=5))))
    assert skewed["comparable"] is False and "incompatible" in skewed["why_not"]
    other_day = F.nq_vs_es(state(inst("NQ"), inst("ES", baseline="2026-10-19")))
    assert other_day["comparable"] is False


# --------------------------------------------------------------------------- megacaps


def test_megacaps_count_only_live_quotes():
    out = F.megacap_participation(state(
        inst("NVDA", change_pct=1.2, asset=AssetClass.EQUITY),
        inst("MSFT", change_pct=-0.4, asset=AssetClass.EQUITY),
        inst("AAPL", change_pct=0.01, asset=AssetClass.EQUITY),
        inst("AMZN", change_pct=2.0, status=DataStatus.STALE, asset=AssetClass.EQUITY),
        inst("META", change_pct=0.9, status=DataStatus.STALE, closed=True,
             asset=AssetClass.EQUITY),
    ))
    assert out["summary"] == ("1 up, 1 down, 1 flat of 3 with live quotes "
                              "(5 not counted: closed, stale or unavailable)")
    by = {s["stock"]: s for s in out["stocks"]}
    assert by["AMZN"] == {"stock": "AMZN", "status": "stale", "change_today": "+2.00%",
                          "as_of": "Tue Oct 20, 10:50 ET"}
    assert by["META"]["status"] == "closed"
    assert by["TSLA"] == {"stock": "TSLA", "status": "unavailable"}
    assert [s["stock"] for s in out["stocks"]] == list(F.MEGACAPS)


def test_megacaps_thin_or_missing():
    stale_only = F.megacap_participation(state(
        inst("NVDA", status=DataStatus.STALE, asset=AssetClass.EQUITY)))
    assert stale_only["summary"] == "not available: no live megacap quotes"
    assert F.megacap_participation(state()) == {"summary": "not available: no megacap quotes"}


def test_participation_only_for_us_index_futures():
    assert set(F.participation(state(), "NQ")) == {"nq_vs_es", "megacap_participation"}
    assert set(F.participation(state(), "ES")) == {"nq_vs_es", "megacap_participation"}
    assert F.participation(state(), "BTC") == {}


# --------------------------------------------------------------------------- events


def test_events_in_window_include_the_lead_and_state_what_was_checked():
    rows = cpi_rows()  # 08:30 ET
    out = F.events_in_window(rows, start=RELEASE + timedelta(minutes=10),
                             end=RELEASE + timedelta(minutes=70), lead=timedelta(minutes=15))
    assert out["checked_from"] == "Tue Oct 20, 08:25 ET"
    assert out["events"] == [{"when": "Tue Oct 20, 08:30 ET", "what": "Consumer prices (CPI)",
                              "importance": "critical"}]
    late = F.events_in_window(rows, start=RELEASE + timedelta(minutes=20),
                              end=RELEASE + timedelta(minutes=80), lead=timedelta(minutes=15))
    assert late["events"] == [] and late["checked_to"] == "Tue Oct 20, 09:50 ET"


async def test_move_note_lists_the_event_in_its_window():
    now = RELEASE + timedelta(minutes=80)
    nq = [SimpleNamespace(timestamp=now - timedelta(minutes=80) + timedelta(minutes=i),
                          value=30000.0 * (1 - 0.0002 * i)) for i in range(80)]
    service, _, _ = make(now, series={"NQ": nq}, rows=cpi_rows())
    await service.tick()
    facts = next(f for k, f in service.writer.calls if k is NoteKind.MOVE)
    events = facts["events_in_move_window"]["events"]
    assert [e["what"] for e in events] == ["Consumer prices (CPI)"]


# --------------------------------------------------------------------------- headlines


def item(source, title, published, received=None):
    return SimpleNamespace(source=source, headline=title, published_at=published,
                           received_at=received or published, source_url=None)


def test_headlines_rank_categorised_first_with_stable_ties_and_cap():
    at = NOW
    news = [item("cnbc_markets", f"Stocks drift {i}", at - timedelta(minutes=i))
            for i in range(10)]
    news += [item("cnbc_markets", f"Fed's Powell speaks {i}", at - timedelta(hours=2, minutes=i))
             for i in range(4)]
    # Same time: ordered by source, then title, whatever the input order.
    news += [item("cnbc_top", "Oil jumps on OPEC cut", at - timedelta(hours=3)),
             item("cnbc_economy", "CPI hotter than expected", at - timedelta(hours=3))]
    service, _, _ = make(at, news=news)
    titles = [h.title for h in service.headlines(at - timedelta(hours=6), at)]
    assert len(titles) == 12
    assert titles[:6] == ["Fed's Powell speaks 0", "Fed's Powell speaks 1",
                          "Fed's Powell speaks 2", "Fed's Powell speaks 3",
                          "CPI hotter than expected", "Oil jumps on OPEC cut"]
    assert titles[6:] == [f"Stocks drift {i}" for i in range(6)]
    reversed_service, _, _ = make(at, news=list(reversed(news)))
    assert [h.title for h in reversed_service.headlines(at - timedelta(hours=6), at)] == titles


def test_duplicate_titles_keep_the_same_copy_whatever_the_input_order():
    at = NOW
    fillers = [item("cnbc_markets", f"Stocks drift {i}", at - timedelta(minutes=10 + i))
               for i in range(12)]
    duplicates = [
        # Only the tech-feed copy is categorised, so it survives the cap.
        item("cnbc_markets", "Retailers slip", at - timedelta(hours=5)),
        item("cnbc_tech", "Retailers slip", at - timedelta(hours=5)),
        # The newer copy wins.
        item("cnbc_top", "Dollar edges lower", at - timedelta(minutes=30)),
        item("cnbc_top", "Dollar edges lower", at - timedelta(minutes=1)),
        # Same time: a publication time beats a received-only time.
        item("cnbc_top", "Bond yields dip", None, received=at - timedelta(minutes=2)),
        item("cnbc_top", "Bond yields dip", at - timedelta(minutes=2)),
        # Same time, both published: ordered by source.
        item("cnbc_top", "Gold steadies", at - timedelta(minutes=3)),
        item("cnbc_markets", "gold steadies", at - timedelta(minutes=3)),
    ]

    def lines(news):
        service, _, _ = make(at, news=news)
        return [(n.headline.source, n.headline.title, n.headline.published_at, n.published,
                 n.categorised) for n in service.news_lines(at - timedelta(hours=6), at)]

    news = fillers + duplicates
    expected = lines(news)
    assert len(expected) == 12
    assert expected[:5] == [
        ("cnbc_tech", "Retailers slip", at - timedelta(hours=5), True, True),
        ("cnbc_top", "Dollar edges lower", at - timedelta(minutes=1), True, False),
        ("cnbc_top", "Bond yields dip", at - timedelta(minutes=2), True, False),
        ("cnbc_markets", "gold steadies", at - timedelta(minutes=3), True, False),
        ("cnbc_markets", "Stocks drift 0", at - timedelta(minutes=10), True, False),
    ]
    assert lines(list(reversed(news))) == expected
    assert lines(duplicates[::-1] + fillers[::-1]) == expected
    assert lines(duplicates[1::2] + fillers + duplicates[::2]) == expected


def test_headline_facts_keep_received_apart_from_published():
    at = NOW
    service, _, _ = make(at, news=[
        item("cnbc_markets", "Nasdaq rises", at - timedelta(minutes=5)),
        item("fed_speeches", "Speech text", None, received=at - timedelta(minutes=9)),
    ])
    facts = service._headline_facts(service.news_lines(at - timedelta(hours=1), at))
    assert facts == [
        {"time": "Tue Oct 20, 10:55 ET", "source": "cnbc_markets", "headline": "Nasdaq rises"},
        {"received": "Tue Oct 20, 10:51 ET", "source": "fed_speeches",
         "headline": "Speech text"},
    ]


# --------------------------------------------------------------------------- fear gauges


def test_each_instrument_reads_its_own_fear_gauge():
    vix = SimpleNamespace(value=18.39, timestamp=NOW)
    vxn = SimpleNamespace(value=26.4, timestamp=NOW)
    nq = F.normal_move(size(), vxn, vix, trigger_pct=0.8, trigger_scaled=True)
    assert nq["vxn"] == "26.40" and nq["vxn_minus_vix"] == "+8.01 points"
    gauge, benchmark = F.VOL_INDEXES["ES"]
    es = F.normal_move(size(), vix, None, trigger_pct=0.8, trigger_scaled=True,
                       gauge=gauge, benchmark=benchmark)
    assert es["vix"] == "18.39" and es["vix_as_of"] == "Tue Oct 20 11:00 ET"
    assert not {"vxn", "vxn_as_of", "vxn_minus_vix"} & es.keys()


async def test_es_notes_get_vix_not_vxn():
    service, _, _ = make(NOW, rows=cpi_rows())
    service.sources.market_state().instruments.update(
        VXN=SimpleNamespace(value=26.4, timestamp=NOW),
        VIX=SimpleNamespace(value=18.39, timestamp=NOW))
    service.sources = service.sources.__class__(**{**service.sources.__dict__,
                                                   "ohlc": lambda *a, **k: []})
    es = service._nq_context(NOW, Instrument.of("ES"))
    nq = service._nq_context(NOW, Instrument.of("NQ"))
    assert es["normal_move_size"]["vix"] == "18.39" and "vxn" not in es["normal_move_size"]
    assert nq["normal_move_size"]["vxn"] == "26.40"
    btc = service._nq_context(NOW, Instrument.of("BTC"))
    assert "nq_vs_es" not in btc
    assert not {"vxn", "vix"} & btc.get("normal_move_size", {}).keys()
