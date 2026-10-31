"""Rebuild full writer facts for stored notes, offline and without AI.

Notes written before facts were captured with them (Phase 1 slice 1) keep only
partial facts. ``rebuild`` replays the desk's own builders at each note's
creation time against *copies* of the stores, with a writer that records the
facts and never makes a call, so a case holds what today's desk would send for
that moment next to the draft that was actually written.

What the replay can and cannot see is recorded in each case's envelope
(``reconstructed``): quotes, headlines and alerts after the note's time are
cut off; macro trends (only the current state is stored), upcoming earnings
and auctions (network calendars) are left out; calendar rows are as stored
now, so a later revision can show.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import tempfile
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from core.desk.evaluation.cases import Case
from core.desk.identity import INSTRUMENT_KINDS, Instrument, instrument_of
from core.desk.models import NoteKind
from core.desk.prompts import PROMPT_VERSION
from core.desk.service import DeskService, DeskSources
from core.desk.store import DeskStore
from core.desk.writer import WriteResult

#: Recorded in every rebuilt case's envelope.
LIMITS = ("facts rebuilt offline by the slice-4 builders, not captured when written",
          "quotes, headlines and alerts after the note's time are excluded",
          "macro_trends omitted: only the current macro state is stored",
          "earnings and Treasury auction calendars omitted (network sources)",
          "calendar rows as stored at rebuild time (later revisions can show)",
          "levels use hourly bars up to the note's time (bars cannot be cut by arrival)")
#: How far before a note's creation its job is looked for (the write takes time).
JOB_SEARCH = tuple(timedelta(seconds=30 * i) for i in range(21))


class _Recorder:
    """A writer that records the facts and returns no draft (never calls out)."""

    ready = True
    detail = None
    model = "rebuild"

    def __init__(self) -> None:
        self.facts: dict[str, Any] | None = None

    def envelope(self, kind, result, *, instrument):
        return {}

    async def write(self, kind, facts, *, instrument):
        self.facts = facts
        return WriteResult(None, "rebuild", None, error="rebuild: no call made")


def _cut_store(desk_db: Path, at: datetime, folder: Path) -> DeskStore:
    """A copy of the desk store as it stood at ``at`` (later notes and alerts removed)."""
    path = folder / "desk.sqlite3"
    path.unlink(missing_ok=True)
    src = sqlite3.connect(f"{desk_db.resolve().as_uri()}?mode=ro", uri=True)
    dst = sqlite3.connect(path)
    try:
        src.backup(dst)
        dst.execute("DELETE FROM desk_notes WHERE created_at >= ?", (at.isoformat(),))
        dst.execute("DELETE FROM desk_alerts WHERE created_at > ?", (at.isoformat(),))
        dst.commit()
    finally:
        src.close()
        dst.close()
    return DeskStore(path)


def _before(fn: Callable[..., Any], at: datetime) -> Callable[..., Any]:
    """Quote history as the desk could read it at ``at``: stamped and received by then."""
    def read(symbol, *, start=None, end=None, limit=500):
        end = at if end is None else min(end, at)
        rows = [o for o in fn(symbol, start=start, end=end, limit=limit + 500)
                if getattr(o, "received_at", None) is None or o.received_at <= at]
        return sorted(rows, key=lambda o: o.timestamp)[-limit:]
    return read


#: A rebuilt move may start this far from the stored one: stored quotes are
#: thinned (repeated polls removed), so the exact start quote can be gone.
MOVE_START_TOLERANCE = timedelta(minutes=3)


async def _facts_for(service: DeskService, note_id: str, kind: NoteKind, created: datetime,
                     move_start: datetime | None = None) -> tuple[dict[str, Any], datetime]:
    recorder: _Recorder = service.writer  # type: ignore[assignment]
    if kind is NoteKind.ASK:
        await service._ask(note_id, created, Instrument.of(instrument_of(note_id)))
        return recorder.facts or {}, created
    starts: list[datetime] = []
    build_move = service.build_move

    async def spy(nid, move, now, inst=None):
        starts.append(move.start_at)
        return await build_move(nid, move, now, inst) if inst else await build_move(nid, move, now)

    service.build_move = spy  # type: ignore[method-assign]
    best: tuple[timedelta, dict[str, Any], datetime] | None = None
    for back in JOB_SEARCH:
        now = created - back
        service.clock = lambda now=now: now
        for job_id, _, build in await service.due_jobs(now):
            if job_id != note_id:
                continue
            recorder.facts, starts[:] = None, []
            await build()
            if recorder.facts is None:
                raise RuntimeError(f"{note_id}: the builder sent no facts")
            if move_start is None:
                return recorder.facts, now
            # A move must be the one the note was written about.
            gap = abs(starts[0] - move_start)
            if gap <= MOVE_START_TOLERANCE and (best is None or gap < best[0]):
                best = (gap, recorder.facts, now)
    if best is not None:
        return best[1], best[2]
    raise LookupError(f"{note_id}: no job of that id is due at {created.isoformat()}")


def rebuild(note_ids: Sequence[str], *, desk_db: Path,
            sources_at: Callable[[datetime], DeskSources]) -> list[Case]:
    """Cases for ``note_ids`` with facts rebuilt at each note's creation time.

    ``sources_at(t)`` gives the desk's read seams as they stood at ``t``.
    """
    db = sqlite3.connect(f"{desk_db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = {}
        for note_id in note_ids:
            row = db.execute("SELECT kind, created_at, payload FROM desk_notes "
                             "WHERE id=? AND status='ok'", (note_id,)).fetchone()
            if row is None:
                raise KeyError(f"no written note {note_id}")
            rows[note_id] = row
    finally:
        db.close()
    cases = []
    with tempfile.TemporaryDirectory() as tmp:
        for note_id, (kind_text, created_at, payload_text) in rows.items():
            kind, payload = NoteKind(kind_text), json.loads(payload_text)
            created = datetime.fromisoformat(created_at)
            store = _cut_store(desk_db, created, Path(tmp))
            try:
                service = DeskService(sources=sources_at(created), store=store,
                                      writer=_Recorder(), clock=lambda c=created: c)
                move_start = (datetime.fromisoformat(payload["event_at"])
                              if kind is NoteKind.MOVE else None)
                facts, at = asyncio.run(_facts_for(service, note_id, kind, created, move_start))
            finally:
                store.close()
            model, _, version = str(payload.get("model") or "").partition(" · ")
            envelope: dict[str, Any] = {
                "prompt_version": PROMPT_VERSION,
                "reconstructed": {"built_at": at.isoformat(), "limits": list(LIMITS)}}
            if kind in INSTRUMENT_KINDS:
                envelope["instrument"] = instrument_of(note_id)
            cases.append(Case(
                id=note_id, kind=kind.value, title=payload.get("title") or "",
                created_at=created_at, facts=json.loads(json.dumps(facts, default=str)),
                facts_complete=True, envelope=envelope,
                baseline={"prompt_version": version or None, "model": model,
                          "draft": payload["draft"]}))
    return cases


def live_sources(*, market_db: Path, events_db: Path, economics_db: Path, work: Path,
                 policy: Any) -> Callable[[datetime], DeskSources]:
    """Read seams over copies of the live stores, cut off at a moment.

    The DuckDB file (and its WAL) is copied into ``work`` first; the running
    service holds a lock on the original. ``events_db`` and ``economics_db``
    must already be copies (``sqlite3 … .backup``): opening a store may migrate it.
    ``policy`` is the running service's ``StalePolicy`` (see ``stale_policy``).
    """
    from core.domain.economics import EconomicsMode, MacroRegistry
    from core.domain.macro_registry import MACRO_DEFINITIONS
    from core.services.economics import EconomicService
    from core.services.market_state import build_market_state
    from core.storage.duckdb_repo import DuckDBObservationRepository
    from core.storage.economics import SQLiteEconomicRepository
    from core.storage.events import SQLiteEventRepository

    work.mkdir(parents=True, exist_ok=True)
    market_copy = work / market_db.name
    shutil.copyfile(market_db, market_copy)
    wal = market_db.with_name(market_db.name + ".wal")
    if wal.exists():
        shutil.copyfile(wal, market_copy.with_name(market_copy.name + ".wal"))
    market = DuckDBObservationRepository(market_copy)
    news = SQLiteEventRepository(events_db, "live")
    economics_repo = SQLiteEconomicRepository(economics_db, EconomicsMode.LIVE)
    for store in (market, news, economics_repo):
        store.initialize()

    def at(moment: datetime) -> DeskSources:
        economics = EconomicService(repository=economics_repo, mode=EconomicsMode.LIVE,
                                    registry=MacroRegistry(definitions=MACRO_DEFINITIONS),
                                    clock=lambda: moment)
        economics._seed()
        history = _before(market.history, moment)

        def state():
            latest = {}
            for symbol in _symbols():
                # The newest quote that had arrived by ``moment``.
                rows = history(symbol, start=moment - timedelta(days=4), end=moment, limit=1)
                if rows:
                    latest[symbol] = rows[-1]
            return build_market_state(latest, policy=policy, now=moment, provider="cnbc")

        def news_events():
            return [e for e in news.query_events(start=moment - timedelta(days=3), end=moment,
                                                 limit=5000)
                    if e.received_at <= moment]

        return DeskSources(
            market_state=state, history=history,
            calendar_events=lambda start, end: economics.calendar_events(
                start=start, end=end, limit=2000),
            news_events=news_events,
            ohlc=lambda symbol, *, end, **kw: market.ohlc(symbol, end=min(end, moment), **kw),
            macro_series=None)

    return at


def stale_policy(settings: Any) -> Any:
    """The live service's freshness thresholds (as ``core.api.app.build_service``)."""
    from core.services.market_state import StalePolicy

    overrides = dict(settings.stale_overrides)
    if settings.market_data_mode == "live" and not settings.data_provider:
        for asset_class, seconds in settings.market_stale_overrides.items():
            overrides.setdefault(asset_class, seconds)
    return StalePolicy(default_seconds=settings.stale_default_seconds, overrides=overrides)


def _symbols() -> tuple[str, ...]:
    from core.domain.instruments import INSTRUMENTS

    return tuple(INSTRUMENTS)
