from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from core.desk import facts as F

T = datetime(2026, 10, 14, 12, 30, tzinfo=UTC)


def row(code, name, *, at=T, actual=None, consensus=None, previous=None, unit="percent",
        importance="critical", status="released", country="US"):
    return SimpleNamespace(event_code=code, name=name, scheduled_at=at, actual=actual,
                           consensus=consensus, previous=previous, unit=unit,
                           importance=importance, status=status, country=country)


def obs(ts, value):
    return SimpleNamespace(timestamp=ts, value=value)


def history_from(series):
    def history(symbol, *, start=None, end=None, limit=500):
        return [o for o in series.get(symbol, [])
                if (start is None or o.timestamp >= start) and (end is None or o.timestamp <= end)]
    return history


# ------------------------------------------------------------------ figures


def test_actual_is_compared_at_official_rounding():
    # Stored CPI m/m is 0.6485 but the official print is 0.6: in line, not a beat.
    figure = F.build_figure("CPI m/m", "percent", 0.6485480245771867, 0.6, 0.4)
    assert figure.actual_text == "0.6%"
    assert figure.verdict == "in_line"


def test_above_and_below_forecast():
    assert F.build_figure("Core CPI m/m", "percent", 0.3348, 0.2, 0.3).verdict == "above"
    assert F.build_figure("Retail", "percent", 0.04, 0.3, 1.2).verdict == "below"


def test_large_numbers_round_to_thousands():
    figure = F.build_figure("Claims", "value", 199_400.0, 199_000.0, 197_000.0)
    assert figure.actual_text == "199K"
    assert figure.verdict == "in_line"
    homes = F.build_figure("Home sales", "homes/year", 3_990_000.0, 3_950_000.0, None)
    assert homes.actual_text == "3.99M"
    assert homes.verdict == "above"


def test_missing_actual_and_missing_forecast_are_explicit():
    assert F.build_figure("X", "percent", None, 0.2, 0.1).verdict == "not_reported"
    assert F.build_figure("X", "percent", 0.2, None, 0.1).verdict == "no_forecast"
    assert F.build_figure("X", "percent", None, None, None).actual_text == "—"


# ------------------------------------------------------------------ slots


def test_release_slot_merges_sources_and_skips_schedule_only_rows():
    rows = [
        row("INITIAL_CLAIMS", "Unemployment Claims", consensus=199000.0, previous=197000.0,
            unit="value", importance="high"),
        row("INITIAL_CLAIMS", "Unemployment Claims", actual=199000.0, previous=197000.0,
            unit="value", importance="high"),
        row("CPI", "Consumer Price Index", actual=0.65, unit="release"),
        row("CPI", "CPI m/m", actual=0.6485, consensus=0.6, previous=0.4),
    ]
    [slot] = F.release_slots(rows, start=T - timedelta(hours=1), end=T + timedelta(hours=1))
    names = [f.name for f in slot.figures]
    assert names == ["CPI m/m", "Unemployment Claims"]
    claims = slot.figures[1]
    assert (claims.actual, claims.forecast) == (199000.0, 199000.0)
    assert slot.complete
    assert slot.title == "Consumer prices (CPI) & Jobless claims"
    assert not slot.is_fed


def test_medium_only_slots_are_dropped_unless_requested():
    rows = [row("ADP", "ADP Weekly", importance="medium", unit="value", consensus=1.0)]
    window = {"start": T - timedelta(hours=1), "end": T + timedelta(hours=1)}
    assert F.release_slots(rows, **window) == []
    assert len(F.release_slots(rows, **window, min_importance="medium")) == 1


def test_fed_slot_and_cancelled_rows():
    rows = [row("FOMC", "FOMC decision", unit="release"),
            row("CPI", "CPI m/m", status="cancelled", consensus=0.2)]
    [slot] = F.release_slots(rows, start=T - timedelta(hours=1), end=T + timedelta(hours=1))
    assert slot.is_fed
    assert slot.codes == ["FOMC"]
    assert not slot.complete  # no figures; Fed notes do not wait for one


# ------------------------------------------------------------------ moves


def test_measure_moves_uses_last_quote_at_or_before_each_end():
    series = {
        "NQ": [obs(T - timedelta(minutes=1), 30000.0), obs(T + timedelta(minutes=5), 30150.0),
               obs(T + timedelta(minutes=14), 29940.0), obs(T + timedelta(minutes=20), 1.0)],
        "US2Y": [obs(T - timedelta(seconds=5), 4.70), obs(T + timedelta(minutes=10), 4.736)],
        "ES": [obs(T + timedelta(minutes=1), 7000.0)],  # no pre-release baseline
    }
    moves = F.measure_moves(history_from(series), start=T, end=T + timedelta(minutes=15),
                            delayed=frozenset({"NQ"}), symbols=("NQ", "ES", "US2Y"))
    by = {m.symbol: m for m in moves}
    assert set(by) == {"NQ", "US2Y"}
    assert by["NQ"].change == -0.2 and by["NQ"].text == "-0.20%" and by["NQ"].delayed
    assert by["US2Y"].change == 3.6 and by["US2Y"].change_unit == "bp"


def test_measure_moves_over_a_long_busy_window_keeps_the_starting_quote():
    # Like the DuckDB store: the newest `limit` rows in the range. A release-to-close
    # window holds thousands of NQ quotes; the starting quote must still be found
    # (every "by the close" follow-up said "not available" before this).
    ticks = [obs(T - timedelta(seconds=30), 30000.0)]
    ticks += [obs(T + timedelta(seconds=5 * i), 30000.0 + i) for i in range(1, 5000)]

    def store(symbol, *, start=None, end=None, limit=500):
        rows = [o for o in ticks if start <= o.timestamp <= end]
        return rows[-limit:]

    end = T + timedelta(hours=7)
    moves = F.measure_moves(store, start=T, end=end, symbols=("NQ",))
    assert len(moves) == 1
    assert moves[0].start_value == 30000.0 and moves[0].end_value == 34999.0


def test_measure_moves_requires_a_minimum_observed_window():
    series = {"NQ": [obs(T - timedelta(minutes=1), 100.0), obs(T + timedelta(seconds=30), 101.0)]}
    assert F.measure_moves(history_from(series), start=T, end=T + timedelta(minutes=15),
                           symbols=("NQ",)) == []


# ------------------------------------------------------------------ quotes and rates


def inst(value, *, change=None, change_pct=None, feed="live", status="live", ts=T):
    return SimpleNamespace(value=value, change=change, change_pct=change_pct,
                           feed_status=feed, status=status, timestamp=ts, display_name="x")


def test_quote_strip_formats_yields_in_basis_points():
    state = SimpleNamespace(instruments={
        "NQ": inst(29698.5, change_pct=1.0445, feed="delayed"),
        "US10Y": inst(5.073, change=-0.025),
        "VXN": inst(27.0, status="stale"),
    })
    quotes = {q.symbol: q for q in F.quote_strip(state, ("NQ", "US10Y", "MISSING"))}
    assert quotes["NQ"].value_text == "29,698.50"
    assert quotes["NQ"].change_text == "+1.04%" and quotes["NQ"].delayed
    assert quotes["US10Y"].change_text == "-2.5bp" and quotes["US10Y"].direction == "down"
    assert "MISSING" not in quotes


def test_rates_view_curve_and_week_change():
    now = T
    state = SimpleNamespace(instruments={
        "US2Y": inst(4.80, change=0.03), "US10Y": inst(4.70, change=0.01),
    })
    series = {"US2Y": [obs(now - timedelta(days=8), 4.60)],
              "US10Y": [obs(now - timedelta(days=8), 4.65)]}
    view = F.rates_view(state, history_from(series), now=now)
    lines = {line.symbol: line for line in view.lines}
    assert lines["US2Y"].day_text == "+3.0bp"
    assert lines["US2Y"].week_change_bp == 20.0
    assert view.curve_bp == -10.0 and "inverted" in view.curve_text
    assert lines["CURVE"].day_change_bp == -2.0
    assert lines["CURVE"].week_change_bp == -15.0


def test_rates_view_without_yields_is_unavailable_not_zero():
    view = F.rates_view(SimpleNamespace(instruments={}), history_from({}), now=T)
    assert view.curve_bp is None and view.curve_text == "unavailable"
    assert all(line.value is None for line in view.lines)


# --- surprise sizing (October 20, 2026) ---------------------------------------

from core.desk.facts import build_figure, size_surprises, surprise_value  # noqa: E402


def _release(at, actual, forecast, *, name="CPI m/m", code="CPI", unit="percent", previous=0.2):
    return SimpleNamespace(event_code=code, name=name, scheduled_at=at, actual=actual,
                           consensus=forecast, previous=previous, unit=unit,
                           importance="critical", status="released", country="US")


def _history(surprises, *, name="CPI m/m", unit="percent", start=None):
    start = start or datetime(2025, 9, 11, 12, 30, tzinfo=UTC)
    rows = []
    for i, s in enumerate(surprises):
        at = start + timedelta(days=30 * i)
        rows.append(_release(at, round(0.3 + s, 1) if unit == "percent" else 200_000 + s,
                             0.3 if unit == "percent" else 200_000.0, name=name, unit=unit,
                             previous=0.2 if unit == "percent" else 199_000.0))
    return rows


NOW_RELEASE = datetime(2026, 10, 14, 12, 30, tzinfo=UTC)


def test_surprise_is_measured_at_official_precision():
    # 0.6485 stored vs a 0.6 forecast is "in line": no surprise.
    in_line = build_figure("CPI m/m", "percent", 0.6485, 0.6, 0.5)
    assert surprise_value(in_line) == 0.0
    assert surprise_value(build_figure("CPI m/m", "percent", 0.44, 0.3, 0.2)) == 0.1
    assert surprise_value(build_figure("CPI m/m", "percent", None, 0.3, 0.2)) is None
    assert surprise_value(build_figure("CPI m/m", "percent", 0.4, None, 0.2)) is None


def test_bigger_than_usual_surprise_against_its_own_history():
    history = _history([0.1, -0.1, 0.0, 0.1, -0.1, 0.0, 0.1, 0.0, -0.1, 0.1])
    (figure,) = size_surprises([build_figure("CPI m/m", "percent", 0.6, 0.3, 0.2)],
                               history, before=NOW_RELEASE)
    s = figure.surprise
    assert s.value == 0.3 and s.value_text == "+0.3 pt"
    assert (s.size, s.history_count, s.larger_than) == ("bigger", 10, 10)
    assert s.text == ("Bigger than usual surprise above the forecast"
                      " — larger than 10 of the last 10")


def test_typical_and_smaller_surprises():
    history = _history([0.1, -0.2, 0.2, -0.3, 0.3, 0.1])
    (typical,) = size_surprises([build_figure("CPI m/m", "percent", 0.1, 0.3, 0.2)],
                                history, before=NOW_RELEASE)
    assert typical.surprise.size == "typical"
    assert typical.surprise.text.startswith("A typical-sized surprise below the forecast")
    (small,) = size_surprises([build_figure("CPI m/m", "percent", 0.4, 0.3, 0.2)],
                              history, before=NOW_RELEASE)
    assert (small.surprise.size, small.surprise.larger_than) == ("smaller", 0)


def test_not_enough_history_says_so():
    (figure,) = size_surprises([build_figure("CPI m/m", "percent", 0.6, 0.3, 0.2)],
                               _history([0.1, 0.2]), before=NOW_RELEASE)
    assert figure.surprise.size == "not_enough_history"
    assert figure.surprise.text == ("Not enough history yet to size this surprise"
                                    " (2 earlier releases stored)")


def test_history_is_per_figure_only_before_the_release_and_capped():
    # A different metric (y/y) and releases at or after this one never count.
    other = _history([0.5] * 8, name="CPI y/y")
    later = _history([0.5] * 3, start=NOW_RELEASE)
    many = _history([0.1] * 15)
    (figure,) = size_surprises([build_figure("CPI m/m", "percent", 0.4, 0.3, 0.2)],
                               other + later + many, before=NOW_RELEASE)
    assert (figure.surprise.history_count, figure.surprise.larger_than) == (12, 0)
    assert figure.surprise.size == "smaller"


def test_in_line_and_unreported_figures():
    figures = [build_figure("CPI m/m", "percent", 0.3, 0.3, 0.2),
               build_figure("Core CPI m/m", "percent", None, 0.3, 0.2)]
    sized = size_surprises(figures, _history([0.1] * 6), before=NOW_RELEASE)
    assert sized[0].surprise.size == "in_line"
    assert sized[1].surprise is None


def test_thousands_surprise_text():
    history = _history([5_000, -3_000, 8_000, -2_000, 4_000], name="Unemployment Claims",
                       unit="value")
    (figure,) = size_surprises(
        [build_figure("Unemployment Claims", "value", 225_000, 200_000, 199_000)],
        history, before=NOW_RELEASE)
    assert figure.surprise.value_text == "+25K" and figure.surprise.size == "bigger"


def test_quote_strip_marks_a_stale_vix_outside_cboe_hours_as_closed():
    night = datetime(2026, 10, 23, 5, 55, tzinfo=UTC)  # 01:55 ET
    day = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)  # 10:00 ET
    for now, closed in ((night, True), (day, False)):
        state = SimpleNamespace(generated_at=now, instruments={
            "VIX": inst(19.03, change_pct=14.0, status="stale"),
            "NQ": inst(29_000.0, change_pct=0.1, status="stale"),
        })
        quotes = {q.symbol: q for q in F.quote_strip(state, ("VIX", "NQ"))}
        # A closed quote is its last close, not a stale feed (and not an issue).
        assert quotes["VIX"].closed is closed and quotes["VIX"].stale is not closed
        assert quotes["VIX"].closed_text == ("cash closed" if closed else None)
        assert quotes["NQ"].closed is False  # futures are never "cash closed"
        assert quotes["VIX"].explainer and "fear gauge" in quotes["VIX"].explainer


# ------------------------------------------------------------------ revisions


def test_revision_down_in_thousands():
    figure = F.build_figure("ADP", "jobs", 42_000.0, 40_000.0, 38_000.0, 36_000.0)
    assert figure.previous_text == "38K"
    assert figure.revised_previous_text == "36K"
    assert figure.revision_text == "revised down by 2K"


def test_revision_up_and_rounded_to_reported_precision():
    # 229,400 -> 231,600 shows as 229K -> 232K, so the size is 3K, not 2.2K.
    figure = F.build_figure("Claims", "value", 230_000.0, 228_000.0, 229_400.0, 231_600.0)
    assert (figure.previous_text, figure.revised_previous_text) == ("229K", "232K")
    assert figure.revision_text == "revised up by 3K"


def test_percent_revision_in_percentage_points():
    figure = F.build_figure("CPI m/m", "percent", 0.3, 0.2, 0.4, 0.30000000000000004)
    assert figure.revised_previous_text == "0.3%"
    assert figure.revision_text == "revised down by 0.1 percentage points"
    two = F.build_figure("Rate", "percent", 4.1, 4.0, 4.25, 4.5)
    assert two.revision_text == "revised up by 0.25 percentage points"


def test_unitless_revision_keeps_decimals():
    figure = F.build_figure("ISM", "index", 49.0, 49.5, 52.3, 52.0)
    assert figure.revision_text == "revised down by 0.3"


def test_no_revision_is_omitted_not_zero():
    for revised in (None, 38_000.0, 38_200.0):  # missing, same, below reported precision
        figure = F.build_figure("ADP", "jobs", 42_000.0, 40_000.0, 38_000.0, revised)
        assert figure.revised_previous is None
        assert figure.revised_previous_text is None
        assert figure.revision_text is None


def test_revised_value_without_first_report_has_no_size():
    figure = F.build_figure("ADP weekly", "jobs", 20_000.0, None, None, 16_750.0)
    assert figure.revised_previous_text == "17K"
    assert figure.revision_text is None


def test_release_slot_carries_revision():
    rows = [row("ADP", "ADP Non-Farm Employment Change", actual=42_000.0, consensus=40_000.0,
                previous=38_000.0, unit="jobs")]
    rows[0].revised_previous = 36_000.0
    [slot] = F.release_slots(rows, start=T - timedelta(minutes=1), end=T + timedelta(minutes=1),
                             min_importance="high")
    assert slot.figures[0].revision_text == "revised down by 2K"


def test_revision_does_not_change_the_current_figure_rounding():
    figure = F.build_figure("CPI m/m", "percent", 0.6485480245771867, 0.6, 0.4, 0.35)
    assert (figure.actual_text, figure.verdict) == ("0.6%", "in_line")


def test_million_revision_size_matches_shown_values():
    figure = F.build_figure("Homes", "homes/year", 2_000_000.0, None, 1_985_000.0, 1_995_000.0)
    shown = (figure.previous_text, figure.revised_previous_text, figure.revision_text)
    assert shown == ("1.99M", "2.00M", "revised up by 10K")


def test_revision_paired_with_its_own_sources_first_report():
    consensus = row("INITIAL_CLAIMS", "Unemployment Claims", consensus=228_000.0,
                    previous=231_000.0, unit="value")
    consensus.revised_previous = None
    dol = row("INITIAL_CLAIMS", "Unemployment Claims", actual=230_000.0, previous=229_000.0,
              unit="value")
    dol.revised_previous = 231_000.0
    for rows in ([consensus, dol], [dol, consensus]):
        [slot] = F.release_slots(rows, start=T - timedelta(minutes=1),
                                 end=T + timedelta(minutes=1), min_importance="high")
        assert slot.figures[0].previous_text == "229K"
        assert slot.figures[0].revision_text == "revised up by 2K"
