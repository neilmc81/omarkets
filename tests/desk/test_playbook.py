"""Playbook: how NQ reacted to each indicator's beats and misses (counting, no AI)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from core.desk import playbook as P

# Thursdays, 08:30 ET (12:30 UTC); the cash close is 20:00 UTC.
DAYS = [datetime(2026, 10, d, 12, 30, tzinfo=UTC) for d in (1, 8, 15)]
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def obs(ts, value):
    return SimpleNamespace(timestamp=ts, value=value)


def row(at, name="Unemployment Claims", actual=197_000.0, consensus=201_000.0,
        code="INITIAL_CLAIMS", importance="high"):
    return SimpleNamespace(event_code=code, name=name, scheduled_at=at, actual=actual,
                           consensus=consensus, previous=200_000.0, unit="count",
                           importance=importance, status="released", country="US",
                           statement=None)


def store(series):
    """Like DuckDB: the newest `limit` quotes in the range."""
    def history(symbol, *, start=None, end=None, limit=500):
        rows = [o for o in series.get(symbol, []) if start <= o.timestamp <= end]
        return rows[-limit:]
    return history


def reaction(at, first_pct, close_pct, *, y2_bp=0.0):
    close = at.replace(hour=20, minute=0)
    return {
        "NQ": [obs(at - timedelta(seconds=20), 30000.0),
               obs(at + timedelta(minutes=14), 30000.0 * (1 + first_pct / 100)),
               obs(close - timedelta(seconds=30), 30000.0 * (1 + close_pct / 100))],
        "US2Y": [obs(at - timedelta(seconds=5), 4.0),
                 obs(at + timedelta(minutes=14), 4.0 + y2_bp / 100)],
    }


def merged(*parts):
    out: dict[str, list] = {}
    for part in parts:
        for symbol, points in part.items():
            out.setdefault(symbol, []).extend(points)
    return out


def test_counts_reactions_per_verdict_and_says_when_too_few():
    series = merged(reaction(DAYS[0], -0.4, +0.6, y2_bp=-2),
                    reaction(DAYS[1], -0.3, -0.5),
                    reaction(DAYS[2], +0.05, +0.3))
    events = [row(DAYS[0]), row(DAYS[1], actual=205_000.0), row(DAYS[2])]
    book = P.build(events, store(series), now=NOW)

    assert book.since == DAYS[0]
    [claims] = book.indicators
    assert claims.name == "Unemployment Claims" and claims.count == 3 and not claims.enough
    assert "too few to call it a pattern" in claims.text

    below = next(b for b in claims.buckets if b.verdict == "below")
    assert (below.count, below.first_down, below.first_flat) == (2, 1, 1)
    assert below.close_up == 2
    assert below.text == ("Below forecast 2 times. First 15 min: NQ fell 1, barely moved 1. "
                          "By the close: rose 2.")
    above = next(b for b in claims.buckets if b.verdict == "above")
    assert above.count == 1 and above.close_down == 1
    # The summary tile's average and the plot's surprise are computed here, not in the UI.
    assert above.nq_15m_avg == -0.3

    newest, _, oldest = claims.releases
    assert newest.at == DAYS[2] and newest.held == "quiet"
    assert (newest.surprise, newest.surprise_text) == (-4000.0, "−4K")
    assert oldest.held == "reversed" and oldest.us2y_15m.change == -2.0
    assert oldest.nq_close.change == 0.6


def test_releases_without_stored_quotes_or_a_forecast_are_left_out():
    events = [row(DAYS[0]), row(DAYS[1], consensus=None)]
    book = P.build(events, store(reaction(DAYS[1], 0.5, 0.5)), now=NOW)
    assert book.indicators == [] and book.since is None


def test_figures_released_together_share_the_reaction_and_name_each_other():
    events = [row(DAYS[0]),
              row(DAYS[0], name="Retail Sales m/m", actual=0.8, consensus=0.3,
                  code="RETAIL_SALES")]
    events[1].unit = "percent"
    book = P.build(events, store(reaction(DAYS[0], -0.2, 0.1)), now=NOW)
    by_name = {i.name: i for i in book.indicators}
    assert set(by_name) == {"Unemployment Claims", "Retail Sales m/m"}
    assert by_name["Retail Sales m/m"].releases[0].alongside == ["Unemployment Claims"]
    assert by_name["Retail Sales m/m"].releases[0].nq_15m.change == -0.2


def test_close_is_pending_until_the_delayed_feed_has_it():
    at = DAYS[2]
    now = at.replace(hour=17)  # 13:00 ET, before the close
    book = P.build([row(at)], store(reaction(at, -0.3, -0.6)), now=now)
    [release] = book.indicators[0].releases
    assert release.close_pending and release.nq_close is None and release.held is None
    assert "not measured yet 1" in book.indicators[0].buckets[0].text


def test_enough_releases_drop_the_warning():
    days = [datetime(2026, 9, 3, 12, 30, tzinfo=UTC) + timedelta(weeks=i) for i in range(5)]
    series = merged(*(reaction(d, -0.2, -0.2) for d in days))
    book = P.build([row(d) for d in days], store(series), now=NOW)
    assert book.indicators[0].enough and book.indicators[0].text == "5 releases measured."


def test_playbook_endpoint_serves_the_service_result():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from core.api import desk as desk_api

    book = P.build([row(DAYS[0])], store(reaction(DAYS[0], -0.3, 0.2)), now=NOW)

    class Desk:
        async def playbook(self):
            return book

    app = FastAPI()
    app.include_router(desk_api.router)
    app.state.desk = Desk()
    body = TestClient(app).get("/api/v1/desk/playbook").json()
    assert body["min_releases"] == 5
    assert body["indicators"][0]["releases"][0]["nq_15m"]["text"] == "-0.30%"
    app.state.desk = None
    assert TestClient(app).get("/api/v1/desk/playbook").status_code == 503


async def test_service_reads_the_calendar_in_chunks():
    from core.desk.service import DeskService, DeskSources
    from core.desk.store import DeskStore

    calls = []

    def calendar(start, end):
        calls.append((start, end))
        return [r for r in [row(DAYS[0])] if start <= r.scheduled_at < end]

    service = DeskService(
        sources=DeskSources(market_state=lambda: None,
                            history=store(reaction(DAYS[0], -0.3, 0.2)),
                            calendar_events=calendar, news_events=lambda: []),
        store=DeskStore(":memory:"), writer=None, clock=lambda: NOW,
    )
    book = await service.playbook()
    assert len(calls) == 5 and calls[0][0] == NOW - P.LOOKBACK and calls[-1][1] == NOW
    assert all(b - a <= timedelta(days=90) for a, b in calls)
    assert book.indicators[0].count == 1
