from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from core.desk.models import NoteDraft, NoteKind
from core.desk.service import DeskService, DeskSources
from core.desk.store import DeskStore
from core.desk.writer import WriteResult

# Tuesday Oct 20 2026; 12:30 UTC is 08:30 ET.
RELEASE = datetime(2026, 10, 20, 12, 30, tzinfo=UTC)


def draft(headline="Inflation ran hotter than expected"):
    return NoteDraft(headline=headline, summary="Bottom line.", what_it_means="Means.",
                     market_reaction="Reaction.", rates_read="Yields rose.",
                     watch_next=["Claims"], learn="Lesson.", nq_tone="negative")


class FakeWriter:
    ready = True
    detail = None
    model = "fake"

    def __init__(self, results=None):
        self.calls: list[tuple[NoteKind, dict]] = []
        self.results = list(results or [])

    async def write(self, kind, facts):
        self.calls.append((kind, facts))
        if self.results:
            return self.results.pop(0)
        return WriteResult(draft(), "fake", 0.01)


def cpi_rows(actual=0.4):
    return [SimpleNamespace(event_code="CPI", name="CPI m/m", scheduled_at=RELEASE,
                            actual=actual, consensus=0.3, previous=0.2, unit="percent",
                            importance="critical", status="released", country="US",
                            statement=None)]


def make(now, *, rows=None, news=(), series=None, writer=None, threshold=0.8):
    clock = {"now": now}
    series = series or {}

    def history(symbol, *, start=None, end=None, limit=500):
        return [o for o in series.get(symbol, [])
                if (start is None or o.timestamp >= start) and (end is None or o.timestamp <= end)]

    def calendar(start, end):
        return [r for r in (rows or []) if start <= r.scheduled_at < end]

    state = SimpleNamespace(instruments={})
    notes: list[tuple[str, str]] = []

    async def notify(summary, body):
        notes.append((summary, body))

    service = DeskService(
        sources=DeskSources(market_state=lambda: state, history=history,
                            calendar_events=calendar, news_events=lambda: list(news)),
        store=DeskStore(":memory:"),
        writer=writer or FakeWriter(),
        notify=notify,
        clock=lambda: clock["now"],
        move_threshold_pct=threshold,
    )
    return service, clock, notes


async def kinds(service, now):
    return [job[1] for job in await service.due_jobs(now)]


async def test_release_waits_for_delayed_data_then_writes_once():
    service, clock, notes = make(RELEASE + timedelta(minutes=10), rows=cpi_rows())
    assert NoteKind.RELEASE not in await kinds(service, clock["now"])

    clock["now"] = RELEASE + timedelta(minutes=25)
    assert NoteKind.RELEASE not in await kinds(service, clock["now"])  # reaction still arriving

    clock["now"] = RELEASE + timedelta(minutes=27)
    produced = await service.tick()
    release = [n for n in produced if n.kind is NoteKind.RELEASE]
    assert len(release) == 1
    assert release[0].figures[0].verdict == "above"
    assert release[0].event_at == RELEASE
    assert notes.count(("Inflation ran hotter than expected", "Bottom line.")) >= 1

    clock["now"] = RELEASE + timedelta(minutes=35)
    assert NoteKind.RELEASE not in await kinds(service, clock["now"])


async def test_missing_actual_waits_until_the_cutoff():
    service, clock, _ = make(RELEASE + timedelta(minutes=30), rows=cpi_rows(actual=None))
    assert NoteKind.RELEASE not in await kinds(service, clock["now"])
    clock["now"] = RELEASE + timedelta(minutes=46)
    assert NoteKind.RELEASE in await kinds(service, clock["now"])


async def test_old_releases_are_never_backfilled():
    service, clock, _ = make(RELEASE + timedelta(hours=7), rows=cpi_rows())
    assert NoteKind.RELEASE not in await kinds(service, clock["now"])


async def test_failed_write_backs_off_and_stops_after_three_attempts():
    class FailingReleases(FakeWriter):
        async def write(self, kind, facts):
            self.calls.append((kind, facts))
            if kind is NoteKind.RELEASE:
                return WriteResult(None, "fake", None, error="provider returned HTTP 500")
            return WriteResult(draft("Other note"), "fake", 0.01)

    writer = FailingReleases()
    service, clock, notes = make(RELEASE + timedelta(minutes=27), rows=cpi_rows(), writer=writer)
    for minutes in (27, 28, 33, 39, 46):
        clock["now"] = RELEASE + timedelta(minutes=minutes)
        await service.tick()
    release_calls = [c for c in writer.calls if c[0] is NoteKind.RELEASE]
    assert len(release_calls) == 3  # 27, 33 and 39; 28 is inside the backoff
    assert NoteKind.RELEASE not in await kinds(service, clock["now"])
    assert all(s == "Other note" or s.startswith("Omarkets could not write") for s, _ in notes)
    failed = service.store.get("release:20261020T1230:CPI")
    assert failed.status == "failed" and failed.error == "provider returned HTTP 500"
    assert service.store.recent() and all(n.status == "ok" for n in service.store.recent())


async def test_premarket_runs_at_nine_et_after_the_eight_thirty_data():
    service, _, _ = make(RELEASE)
    # 08:55 ET: too early. 09:05 ET: due.
    assert NoteKind.PREMARKET not in await kinds(service, datetime(2026, 10, 20, 12, 55,
                                                                    tzinfo=UTC))
    assert NoteKind.PREMARKET in await kinds(service, datetime(2026, 10, 20, 13, 5, tzinfo=UTC))
    # Saturday: nothing.
    assert NoteKind.PREMARKET not in await kinds(service, datetime(2026, 10, 24, 12, 5,
                                                                    tzinfo=UTC))


async def test_primer_window_is_sunday_evening_to_monday_noon():
    service, _, _ = make(RELEASE)
    sunday = datetime(2026, 10, 25, 21, 30, tzinfo=UTC)  # 17:30 ET
    assert NoteKind.PRIMER in await kinds(service, sunday)
    assert NoteKind.PRIMER not in await kinds(service, datetime(2026, 10, 21, 15, 0,
                                                                tzinfo=UTC))


async def test_large_nq_move_gets_one_note_per_three_hours():
    now = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
    nq = [SimpleNamespace(timestamp=now - timedelta(minutes=80) + timedelta(minutes=i),
                          value=30000.0 * (1 - 0.0002 * i)) for i in range(80)]
    service, clock, _ = make(now, series={"NQ": nq})
    produced = await service.tick()
    assert [n.kind for n in produced] == [NoteKind.MOVE]
    assert produced[0].title.startswith("NQ down")
    clock["now"] = now + timedelta(hours=1, minutes=5)
    assert NoteKind.MOVE not in await kinds(service, clock["now"])


async def test_small_move_is_ignored():
    now = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
    nq = [SimpleNamespace(timestamp=now - timedelta(minutes=80) + timedelta(minutes=i),
                          value=30000.0 + i) for i in range(80)]
    service, _, _ = make(now, series={"NQ": nq})
    assert NoteKind.MOVE not in await kinds(service, now)


def news(source, headline, at):
    return SimpleNamespace(source=source, headline=headline, published_at=at,
                           received_at=at, source_url=None)


def test_world_news_needs_a_market_keyword():
    at = RELEASE
    service, _, _ = make(RELEASE, news=[
        news("guardian_world", "Festival crush raises safety concerns", at),
        news("guardian_world", "Iran fires missiles at shipping lanes", at),
        news("cnbc_markets", "Stocks slip as yields climb", at),
        news("cnbc_markets", "Stocks slip as yields climb", at),
    ])
    titles = [h.title for h in service.headlines(at - timedelta(hours=1), at)]
    assert titles == ["Iran fires missiles at shipping lanes", "Stocks slip as yields climb"]


async def test_ask_has_a_daily_limit():
    service, clock, notes = make(datetime(2026, 10, 20, 15, 0, tzinfo=UTC))
    assert service.ask_daily_limit == 5
    service.ask_daily_limit = 1
    note = await service.ask()
    assert note.kind is NoteKind.ASK and note.status == "ok"
    assert notes == []  # on-demand notes are not pushed as notifications
    clock["now"] += timedelta(minutes=16)
    with pytest.raises(RuntimeError):
        await service.ask()


async def test_ask_reuses_a_note_from_the_last_15_minutes():
    service, clock, _ = make(datetime(2026, 10, 20, 15, 0, tzinfo=UTC))
    first = await service.ask()
    clock["now"] += timedelta(minutes=14)
    assert (await service.ask()).id == first.id
    clock["now"] += timedelta(minutes=2)
    assert (await service.ask()).id != first.id
    assert service.store.count_since("ask", "2026-10-20T00:00:00+00:00") == 2


async def test_view_lists_notes_and_session():
    service, _, _ = make(RELEASE + timedelta(minutes=27), rows=cpi_rows())
    await service.tick()
    view = await service.view()
    assert view.notes and view.writer_ready
    assert view.session_text.startswith("Futures trading overnight")


def medium_row(name, *, consensus=None, unit="percent", at=RELEASE):
    return SimpleNamespace(event_code="DURABLE_GOODS", name=name, scheduled_at=at, actual=0.5,
                           consensus=consensus, previous=0.1, unit=unit, importance="medium",
                           status="released", country="US", statement=None)


async def test_medium_releases_get_notes_but_schedule_only_rows_do_not():
    rows = [medium_row("Durable Goods m/m", consensus=0.3)]
    service, _, _ = make(RELEASE + timedelta(minutes=27), rows=rows)
    assert NoteKind.RELEASE in await kinds(service, RELEASE + timedelta(minutes=27))

    bare = [medium_row("BEA release", unit="release")]
    service, _, _ = make(RELEASE + timedelta(minutes=50), rows=bare)
    assert NoteKind.RELEASE not in await kinds(service, RELEASE + timedelta(minutes=50))


async def test_premarket_leads_with_this_mornings_data():
    writer = FakeWriter()
    now = datetime(2026, 10, 20, 13, 5, tzinfo=UTC)  # 09:05 ET
    service, _, _ = make(now, rows=cpi_rows(), writer=writer)
    produced = await service.tick()
    premarket = next(n for n in produced if n.kind is NoteKind.PREMARKET)
    facts = next(f for k, f in writer.calls if k is NoteKind.PREMARKET)
    assert facts["released_this_morning"][0]["figures"][0]["versus_forecast"] == "above"
    assert premarket.figures[0].name == "CPI m/m"


async def test_note_that_fails_every_retry_alerts_once():
    class Failing(FakeWriter):
        async def write(self, kind, facts):
            if kind is NoteKind.RELEASE:
                return WriteResult(None, "fake", None, error="provider returned HTTP 500")
            return WriteResult(draft("Other note"), "fake", 0.01)

    service, clock, notes = make(RELEASE + timedelta(minutes=27), rows=cpi_rows(),
                                 writer=Failing())
    for minutes in (27, 33):
        clock["now"] = RELEASE + timedelta(minutes=minutes)
        await service.tick()
    assert not [s for s, _ in notes if s.startswith("Omarkets could not write")]
    for minutes in (39, 46, 60):  # third failure, then later ticks
        clock["now"] = RELEASE + timedelta(minutes=minutes)
        await service.tick()
    alerts = [(s, b) for s, b in notes if s.startswith("Omarkets could not write")]
    assert len(alerts) == 1
    assert "HTTP 500" in alerts[0][1] and "3 times" in alerts[0][1]
    # A restart (same store) never re-alerts the same note id.
    assert not service.store.claim_failure_alert("release:20261020T1230:CPI")


def test_store_upgrades_an_old_table_with_the_alert_flag(tmp_path):
    import sqlite3

    path = tmp_path / "desk.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE desk_notes (id TEXT PRIMARY KEY, kind TEXT NOT NULL,"
               " status TEXT NOT NULL, created_at TEXT NOT NULL,"
               " attempts INTEGER NOT NULL DEFAULT 1, payload TEXT NOT NULL)")
    db.commit()
    db.close()
    store = DeskStore(path)
    assert not store.claim_failure_alert("missing")
    store.close()


async def test_release_note_sizes_the_surprise_and_tells_the_writer():
    past = [SimpleNamespace(event_code="CPI", name="CPI m/m",
                            scheduled_at=RELEASE - timedelta(days=30 * (i + 1)),
                            actual=0.3, consensus=0.3, previous=0.2, unit="percent",
                            importance="critical", status="released", country="US",
                            statement=None) for i in range(6)]
    writer = FakeWriter()
    service, clock, _ = make(RELEASE + timedelta(minutes=27), rows=cpi_rows() + past,
                             writer=writer)
    await service.tick()
    stored = service.store.get("release:20261020T1230:CPI")
    assert stored.figures[0].surprise.size == "bigger"
    facts = next(f for kind, f in writer.calls if kind is NoteKind.RELEASE)
    assert facts["figures"][0]["surprise"] == "+0.1 pt"
    assert "larger than 6 of the last 6" in facts["figures"][0]["surprise_size"]


def _learn_note(note_id, learn, at, *, status="ok"):
    from core.desk.models import DeskNote

    return DeskNote(id=note_id, kind=NoteKind.RELEASE, title=f"Title {note_id}",
                    created_at=at, status=status,
                    draft=draft().model_copy(update={"learn": learn}) if status == "ok" else None)


def test_learn_library_dedupes_and_keeps_the_newest_source():
    store = DeskStore(":memory:")
    store.save(_learn_note("a", "CPI measures consumer prices.", RELEASE))
    store.save(_learn_note("b", "cpi  measures consumer prices", RELEASE + timedelta(days=30)))
    store.save(_learn_note("c", "The 2-year yield tracks Fed expectations.",
                           RELEASE + timedelta(days=1)))
    store.save(_learn_note("d", "", RELEASE, status="failed"))
    entries = store.learn_entries()
    assert [(e.note_id, e.times) for e in entries] == [("b", 2), ("c", 1)]
    assert entries[0].text == "cpi measures consumer prices"


def test_learn_api_lists_the_library():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from core.api import desk as desk_api

    service, _, _ = make(RELEASE)
    service.store.save(_learn_note("a", "Yields and tech stocks move in opposite ways.", RELEASE))
    app = FastAPI()
    app.include_router(desk_api.router)
    app.state.desk = service
    body = TestClient(app).get("/api/v1/desk/learn").json()
    assert body["count"] == 1 and body["entries"][0]["note_id"] == "a"


def _speech(at, *, importance="high", name="Fed testimony: Chairman Kevin Warsh"):
    return SimpleNamespace(event_code="FED_TESTIMONY", name=name, scheduled_at=at, actual=None,
                           consensus=None, previous=None, unit="release",
                           importance=importance, status="scheduled", country="US",
                           statement=None, notes=["Schedule only.", "Venue: Senate Banking"])


async def test_fed_speakers_show_in_coming_up_but_never_get_a_note():
    speech_at = RELEASE + timedelta(hours=2)
    service, clock, _ = make(RELEASE, rows=[_speech(speech_at)])
    upcoming = await service.upcoming(clock["now"], days=7)
    assert [(u.kind, u.title, u.detail) for u in upcoming] == [
        ("fed", "Fed testimony: Chairman Kevin Warsh", "Senate Banking")]
    assert "Chair" in upcoming[0].explainer
    clock["now"] = speech_at + timedelta(minutes=30)
    assert NoteKind.RELEASE not in await kinds(service, clock["now"])
    assert NoteKind.FED not in await kinds(service, clock["now"])


async def test_view_shows_the_next_high_impact_event():
    service, _, _ = make(RELEASE - timedelta(hours=3), rows=cpi_rows(actual=None))
    view = await service.view()
    assert view.next_event is not None
    assert view.next_event.at == RELEASE and view.next_event.importance == "critical"
    assert view.next_event.playbook_text == "No earlier releases measured yet."


async def test_calendar_rows_size_surprises_and_count_hidden_moments():
    past = [SimpleNamespace(**{**vars(cpi_rows()[0]), "scheduled_at": RELEASE - timedelta(
        days=30 * k), "actual": 0.3}) for k in range(1, 7)]
    low = SimpleNamespace(**{**vars(medium_row("Durable Goods Orders m/m",
                                               at=RELEASE + timedelta(hours=2))),
                             "importance": "low"})
    service, _, _ = make(RELEASE + timedelta(hours=3), rows=[*past, *cpi_rows(), low])
    start, end = RELEASE - timedelta(days=1), RELEASE + timedelta(days=1)
    rows, hidden = await service.calendar(start, end, show_all=False)
    assert [r.title for r in rows] == ["Consumer prices (CPI)"] and hidden == 1
    assert rows[0].figures[0].surprise.history_count == 6
    every, none_hidden = await service.calendar(start, end, show_all=True)
    assert len(every) == 2 and none_hidden == 0
