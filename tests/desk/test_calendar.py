"""The Calendar page's view model (deterministic, no AI)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from core.desk import calendar as CAL
from core.desk import playbook as P
from core.desk.links import ReleaseNotes

# Thursday Oct 22 2026, 08:30 ET (21:30 in Tokyo).
CLAIMS = datetime(2026, 10, 22, 12, 30, tzinfo=UTC)
TOKYO = ZoneInfo("Asia/Tokyo")


def row(at, *, code="INITIAL_CLAIMS", name="Unemployment Claims", actual=197_000.0,
        consensus=201_000.0, previous=200_000.0, importance="high", unit="count"):
    return SimpleNamespace(event_code=code, name=name, scheduled_at=at, actual=actual,
                           consensus=consensus, previous=previous, unit=unit,
                           importance=importance, status="released", country="US",
                           statement=None)


def obs(ts, value):
    return SimpleNamespace(timestamp=ts, value=value)


def history_of(series):
    def history(symbol, *, start=None, end=None, limit=500):
        return [o for o in series.get(symbol, []) if start <= o.timestamp <= end][-limit:]
    return history


NQ = {"NQ": [obs(CLAIMS - timedelta(seconds=20), 29_000.0),
             obs(CLAIMS + timedelta(minutes=14), 28_982.6)]}
WEEK = dict(start=CLAIMS - timedelta(days=3), end=CLAIMS + timedelta(days=3))


def build(events, *, now, series=None, notes=(), playbook=None, past=(), show_all=False):
    return CAL.rows(events, history_of(series or {}), **WEEK, now=now,
                    notes=ReleaseNotes(notes), playbook=playbook, surprise_history=past,
                    show_all=show_all)


def test_released_row_has_result_read_move_and_note():
    note = "release:20261022T1230:INITIAL_CLAIMS"
    [r] = build([row(CLAIMS)], now=CLAIMS + timedelta(hours=1), series=NQ, notes=[note])
    [figure] = r.figures
    assert r.released and r.kind == "economic" and r.importance == "high"
    assert (figure.actual_text, figure.forecast_text, figure.previous_text) == (
        "197K", "201K", "200K")
    assert figure.verdict == "below" and figure.surprise.value_text == "−4K"
    # Fewer jobless claims than forecast reads better for the economy.
    assert (figure.reading, figure.reading_text) == ("better", "Stronger jobs data than forecast")
    assert r.nq_15m == -0.06 and r.nq_15m_text == "NQ -0.06% in 15 min"
    assert r.nq_15m_status == "measured" and r.note_id == note
    assert r.explainer_key == "indicator:INITIAL_CLAIMS" and r.explainer


def test_reading_follows_the_figure_family():
    hot = build([row(CLAIMS, code="CPI", name="CPI m/m", actual=0.4, consensus=0.3,
                     previous=0.2, unit="percent")], now=CLAIMS + timedelta(hours=1))
    assert (hot[0].figures[0].reading, hot[0].figures[0].reading_text) == (
        "worse", "Hotter than forecast")
    strong = build([row(CLAIMS, code="RETAIL_SALES", name="Retail Sales m/m", actual=0.5,
                        consensus=0.2, previous=0.1, unit="percent")],
                   now=CLAIMS + timedelta(hours=1))
    assert strong[0].figures[0].reading == "better"
    matched = build([row(CLAIMS, actual=201_000.0)], now=CLAIMS + timedelta(hours=1))
    assert (matched[0].figures[0].reading, matched[0].figures[0].reading_text) == (
        None, "In line with forecast")


def test_mixed_moment_reads_each_figure_by_its_own_code():
    rows = [row(CLAIMS, code="CPI", name="CPI m/m", actual=0.4, consensus=0.3,
                previous=0.2, unit="percent", importance="critical"),
            row(CLAIMS)]
    [r] = build(rows, now=CLAIMS + timedelta(hours=1))
    reads = {f.name: f.reading for f in r.figures}
    assert reads == {"CPI m/m": "worse", "Unemployment Claims": "better"}


def test_missing_actual_or_forecast_stays_unavailable_not_zero():
    no_actual = build([row(CLAIMS, actual=None)], now=CLAIMS + timedelta(hours=1))
    figure = no_actual[0].figures[0]
    assert figure.actual is None and figure.actual_text == "—"
    assert figure.verdict == "not_reported" and figure.surprise is None
    assert figure.reading is None and figure.reading_text is None
    no_forecast = build([row(CLAIMS, consensus=None)], now=CLAIMS + timedelta(hours=1))
    figure = no_forecast[0].figures[0]
    assert figure.forecast is None and figure.forecast_text == "—"
    assert figure.verdict == "no_forecast" and figure.surprise is None


def test_surprise_size_uses_the_figure_history():
    past_at = [CLAIMS - timedelta(days=7 * k) for k in range(1, 7)]
    # Earlier misses of 1K; this week's 4K miss is bigger than all six.
    past = [row(at, actual=200_000.0, consensus=201_000.0) for at in past_at]
    [r] = build([row(CLAIMS)], now=CLAIMS + timedelta(hours=1), past=past)
    surprise = r.figures[0].surprise
    assert surprise.size == "bigger" and surprise.history_count == 6
    assert surprise.larger_than == 6
    [thin] = build([row(CLAIMS)], now=CLAIMS + timedelta(hours=1), past=past[:2])
    assert thin.figures[0].surprise.size == "not_enough_history"


def test_fifteen_minute_move_states():
    upcoming = build([row(CLAIMS, actual=None)], now=CLAIMS - timedelta(minutes=1), series=NQ)
    assert upcoming[0].nq_15m_status == "upcoming" and not upcoming[0].released
    assert upcoming[0].figures[0].surprise is None
    waiting = build([row(CLAIMS)], now=CLAIMS + timedelta(minutes=20), series=NQ)
    assert waiting[0].nq_15m_status == "waiting" and waiting[0].nq_15m is None
    missing = build([row(CLAIMS)], now=CLAIMS + timedelta(hours=1), series={})
    assert missing[0].nq_15m_status == "unavailable" and missing[0].nq_15m is None


def test_market_moving_filter_drops_low_importance():
    rows = [row(CLAIMS), row(CLAIMS + timedelta(hours=2), code="EXISTING_HOME_SALES",
                             name="Existing Home Sales", importance="low")]
    now = CLAIMS + timedelta(hours=3)
    assert [r.at for r in build(rows, now=now)] == [CLAIMS]
    assert len(build(rows, now=now, show_all=True)) == 2


def test_speech_and_fed_rows_have_no_playbook():
    rows = [row(CLAIMS, code="FED_SPEECH", name="Fed Speech", actual=None, consensus=None,
                previous=None, importance="medium", unit="release"),
            row(CLAIMS + timedelta(hours=6), code="FOMC", name="Federal Funds Rate",
                actual=None, consensus=4.25, previous=4.5, unit="percent")]
    speech, fomc = build(rows, now=CLAIMS - timedelta(hours=1),
                         playbook=P.build([], history_of({}), now=CLAIMS))
    assert speech.kind == "speech" and speech.explainer_key == "indicator:FED_SPEECH"
    assert speech.figures == [] and speech.playbook_text is None
    assert fomc.kind == "fed" and fomc.playbook is None and fomc.playbook_text is None


def test_playbook_summary_comes_from_the_cached_playbook():
    past = [CLAIMS - timedelta(days=7 * k) for k in range(1, 4)]
    series = {"NQ": [o for at in past for o in (obs(at - timedelta(seconds=20), 29_000.0),
                                                 obs(at + timedelta(minutes=14), 28_900.0))]}
    book = P.build([row(at) for at in past], history_of(series), now=CLAIMS)
    [r] = build([row(CLAIMS, actual=None)], now=CLAIMS - timedelta(hours=1), playbook=book)
    assert r.playbook is not None and r.playbook.count == 3
    assert r.playbook_text.startswith("Unemployment Claims, first 15 min: Below forecast (3)")
    [none] = build([row(CLAIMS, actual=None)], now=CLAIMS - timedelta(hours=1))
    assert none.playbook is None and none.playbook_text is None


def test_days_group_in_the_reader_time_zone():
    late = CLAIMS + timedelta(hours=5, minutes=30)  # 14:00 ET Thu = 03:00 Fri in Tokyo
    found = build([row(CLAIMS), row(late, code="FOMC", name="Federal Funds Rate",
                                    actual=None, consensus=None)],
                  now=CLAIMS - timedelta(days=1))
    tokyo = CAL.by_day(found, TOKYO)
    assert [(d.date, d.label, len(d.rows)) for d in tokyo] == [
        ("2026-10-22", "Thursday, Oct 22", 1), ("2026-10-23", "Friday, Oct 23", 1)]
    ny = CAL.by_day(found, ZoneInfo("America/New_York"))
    assert [(d.date, len(d.rows)) for d in ny] == [("2026-10-22", 2)]
