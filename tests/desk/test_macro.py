"""The Macro page's view model (deterministic, no AI)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from core.desk import macro as M
from core.desk.models import RatesView, YieldLine

NOW = datetime(2026, 10, 23, 15, 0, tzinfo=UTC)
CPI_AT = datetime(2026, 10, 14, 12, 30, tzinfo=UTC)


def series(sid, values, *, unit="percent", frequency="monthly", transform="yoy",
           provider="bls", status="live", period="2026-09"):
    definition = SimpleNamespace(id=sid, name=sid, unit=unit, frequency=frequency,
                                 transform=transform, provider=provider,
                                 source_series_id=sid + "_SRC", info="Seasonally adjusted")
    obs = [SimpleNamespace(value=v, period=period) for v in values]
    latest = SimpleNamespace(value=values[-1], period=period, released_at=None,
                             received_at=CPI_AT + timedelta(minutes=15), revision=1)
    return SimpleNamespace(definition=definition, latest=latest, recent=obs, status=status)


def event(code, name, at, actual, consensus, unit="percent"):
    return SimpleNamespace(event_code=code, name=name, scheduled_at=at, actual=actual,
                           consensus=consensus, previous=None, unit=unit, importance="critical",
                           status="released", country="US", statement=None, notes=None)


def card(found, key):
    return next(c for g in found.groups for c in g.cards if c.key == key)


def test_trend_compares_recent_averages():
    assert M.trend([1, 1, 1, 2, 2, 2], window=3, steady=0.1) == "rising"
    assert M.trend([2, 2, 2, 1, 1, 1], window=3, steady=0.1) == "falling"
    assert M.trend([2, 2, 2, 2.05, 2, 2], window=3, steady=0.1) == "steady"
    assert M.trend([1, 2, 3], window=3, steady=0.1) == "unknown"


def test_colours_mean_good_or_bad_for_nq():
    assert M.nq_effect("rising", "bad") == "bad"
    assert M.nq_effect("falling", "bad") == "good"
    assert M.nq_effect("rising", "mixed") == "mixed"
    assert M.nq_effect("steady", "bad") == "neutral"


def test_board_has_four_groups_and_reads_cards():
    cpi = series("CPI_YOY", [2.4, 2.5, 2.6, 3.0, 3.2, 3.7])
    events = [event("CPI", "CPI y/y", CPI_AT, 3.7376, 3.6),
              event("CPI", "CPI m/m", CPI_AT, 0.6, 0.6),
              SimpleNamespace(event_code="FOMC", name="FOMC decision", unit="release",
                              scheduled_at=datetime(2026, 10, 28, 18, 0, tzinfo=UTC),
                              actual=None, consensus=None, previous=None,
                              importance="critical", status="scheduled", country="US")]
    rates = RatesView(as_of=NOW, curve_bp=None, curve_text="", explainer="", lines=[
        YieldLine(symbol="US2Y", label="2-year yield", value=4.7, value_text="4.700%",
                  day_change_bp=6.0, week_change_bp=12.0, day_text="+6.0bp",
                  week_text="+12.0bp")])
    found = M.board([cpi], events, rates, now=NOW)
    assert [g.title for g in found.groups] == ["Inflation", "Jobs", "Growth", "The Fed"]
    assert sum(len(g.cards) for g in found.groups) == 14
    c = card(found, "cpi")
    assert (c.value_text, c.period_text, c.trend, c.nq_effect) == ("3.7%", "Sep 2026", "rising",
                                                                    "bad")
    assert c.release.text == "Last release: above forecast"
    assert c.release.figure.name == "CPI y/y" and c.release.figure.reading == "worse"
    assert c.release.nq_effect == "bad" and c.points[-1] == 3.7
    assert any("BLS" in line for line in c.sources)
    # A series the core does not have shows as unavailable, never as zero.
    pce = card(found, "pce")
    assert pce.value is None and pce.value_text == "—" and pce.stale
    nxt = card(found, "fed_next")
    assert nxt.value_text == "Wed Oct 28" and nxt.period_text == "14:00 ET"
    two = card(found, "us2y")
    assert (two.value_text, two.trend, two.nq_effect) == ("4.700%", "rising", "bad")


def test_ppi_duplicates_are_merged_and_named():
    bls = series("BLS_PPI_YOY", [6, 6, 6, 6, 6, 6.3])
    fred = series("PPI", [1, 1, 1, 1, 1, 1.3], provider="fred", transform="mom")
    found = M.board([bls, fred], [], None, now=NOW)
    ppi = card(found, "ppi")
    assert ppi.value_text == "6.3%"
    assert any("merged" in line and "FRED" in line for line in ppi.sources)


def test_release_falls_back_to_the_next_named_figure_and_skips_unscored():
    ppi = series("BLS_PPI_YOY", [6.0] * 6)
    at = datetime(2026, 10, 15, 12, 30, tzinfo=UTC)
    events = [event("PPI", "PPI m/m", at, 1.3, 0.6), event("PPI", "PPI y/y", at, 6.3, None)]
    release = card(M.board([ppi], events, None, now=NOW), "ppi").release
    assert release.figure.name == "PPI m/m" and release.figure.verdict == "above"


def test_payrolls_and_job_openings_formatting():
    payrolls = series("PAYROLLS", [100, 90, 80, 50, 40, 29], unit="thousand jobs",
                      transform="difference")
    jolts = series("JOLTS", [7200, 7100, 7000, 7100, 7050, 7079], unit="thousand jobs",
                   transform="identity", period="2026-08")
    found = M.board([payrolls, jolts], [], None, now=NOW)
    assert card(found, "payrolls").value_text == "+29K"
    assert card(found, "payrolls").nq_effect == "mixed"
    assert card(found, "jolts").value_text == "7.08M"
    assert card(found, "jolts").period_text == "Aug 2026"


# ------------------------------------------------------------------ writer facts


def fixture_board():
    cpi = series("CPI_YOY", [2.4, 2.5, 2.6, 3.0, 3.2, 3.7])
    stale = series("UNEMPLOYMENT", [4.3, 4.3, 4.2, 4.2, 4.2, 4.2], status="stale")
    events = [event("CPI", "CPI y/y", CPI_AT, 3.7376, 3.6),
              SimpleNamespace(event_code="FOMC", name="FOMC decision", unit="release",
                              scheduled_at=datetime(2026, 10, 28, 18, 0, tzinfo=UTC),
                              actual=None, consensus=None, previous=None,
                              importance="critical", status="scheduled", country="US")]
    rates = RatesView(as_of=NOW, curve_bp=None, curve_text="", explainer="", lines=[
        YieldLine(symbol="US2Y", label="2-year yield", value=4.7, value_text="4.700%",
                  day_change_bp=6.0, week_change_bp=12.0, day_text="+6.0bp",
                  week_text="+12.0bp")])
    return M.board([cpi, stale], events, rates, now=NOW)


def test_writer_trends_are_ready_made_text_by_group():
    trends = M.writer_trends(fixture_board())
    assert list(trends) == ["inflation", "jobs"]
    assert trends["inflation"] == [{
        "name": "CPI year over year", "latest": "3.7% (Sep 2026)",
        "trend": "rising: the last 3 months averaged higher than the 3 before",
        "for_nq": "a rise usually weighs on NQ",
        "last_release": "CPI y/y above forecast (Oct 14)"}]
    [jobless] = trends["jobs"]
    assert jobless["stale"] is True and jobless["for_nq"] == "a rise cuts both ways for NQ"
    assert "last_release" not in jobless


def test_writer_trends_leave_out_missing_and_duplicated_cards():
    trends = M.writer_trends(fixture_board())
    names = [row["name"] for rows in trends.values() for row in rows]
    # No value (PCE etc.) → left out, never zero; the 2-year yield and next Fed
    # date are already in the writer's facts; no Fed target series here.
    assert names == ["CPI year over year", "Unemployment rate"]
    assert "fed" not in trends


def test_writer_trend_numbers_pass_the_number_check():
    from core.desk.models import NoteDraft, NoteKind
    from core.desk.prompts import SYSTEM, user_message
    from core.desk.writer import invented_numbers

    message = user_message(NoteKind.PRIMER, {"macro_trends": M.writer_trends(fixture_board())})
    draft = NoteDraft(
        headline="Inflation is still rising", summary="CPI is 3.7%, up over 3 months.",
        what_it_means="Unemployment is 4.2%.", market_reaction="Quiet.",
        rates_read="Yields flat.", learn="CPI measures prices.", nq_tone="neutral")
    assert invented_numbers(draft, SYSTEM + message) == []


def test_writer_last_release_names_a_different_figure_and_an_older_date():
    ppi = series("BLS_PPI_YOY", [2.0, 2.0, 2.0, 2.0, 2.0, 2.1])
    older = CPI_AT - timedelta(days=30)
    events = [event("PPI", "PPI m/m", CPI_AT, 0.5, 0.2),            # only m/m had a forecast
              event("CPI", "CPI y/y", older, 3.5, 3.4),
              event("CPI", "CPI y/y", CPI_AT, 3.7, None)]           # newest: no forecast
    cpi = series("CPI_YOY", [2.4, 2.5, 2.6, 3.0, 3.2, 3.7])
    trends = M.writer_trends(M.board([ppi, cpi], events, None, now=NOW))
    rows = {row["name"]: row["last_release"] for row in trends["inflation"]}
    assert rows["PPI year over year"] == "PPI m/m above forecast (Oct 14)"
    assert rows["CPI year over year"] == "CPI y/y above forecast (Sep 14)"
