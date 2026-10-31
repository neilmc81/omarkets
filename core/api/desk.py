"""Desk API: the Today screen's single read, one note, and "explain now"."""

from __future__ import annotations

from datetime import UTC, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import AwareDatetime

from core.api.economics import calendar_coverage
from core.desk import calendar as CAL
from core.desk import lessons as LS
from core.desk.identity import LEGACY, Instrument
from core.desk.models import (
    CalendarBoard,
    DeskNote,
    DeskView,
    LearnLibrary,
    MacroBoard,
    NewsBoard,
    NoteList,
    NoteSummary,
    Playbook,
)
from core.desk.service import DeskService
from core.services.economics import calendar_window

router = APIRouter(prefix="/api/v1", tags=["desk"])


def get_desk(request: Request) -> DeskService:
    desk = getattr(request.app.state, "desk", None)
    if desk is None:
        raise HTTPException(status_code=503, detail="Desk is not enabled")
    return desk


DeskDep = Annotated[DeskService, Depends(get_desk)]

#: Without ``from``, the list covers this long before ``to``.
NOTES_WINDOW = timedelta(days=7)


@router.get("/desk", response_model=DeskView)
async def desk_view(desk: DeskDep, limit: int = Query(default=30, ge=1, le=100)) -> DeskView:
    return await desk.view(limit=limit)


@router.get("/desk/learn", response_model=LearnLibrary)
async def desk_learn(desk: DeskDep) -> LearnLibrary:
    """Every note's Learn box, newest first, with repeated lessons merged."""
    return LS.library(desk.store.learn_entries())


@router.get("/desk/playbook", response_model=Playbook)
async def desk_playbook(desk: DeskDep) -> Playbook:
    """How NQ reacted when each indicator beat, missed or matched its forecast."""
    return await desk.playbook()


@router.get("/desk/calendar", response_model=CalendarBoard)
async def desk_calendar(
    request: Request,
    desk: DeskDep,
    week: Literal["this", "next"] = "this",
    show_all: Annotated[bool, Query(alias="all")] = False,
    tz: Annotated[str | None, Query(max_length=64)] = None,
) -> CalendarBoard:
    """A New York week of release moments grouped by day in ``tz`` (default: the
    desk's reader time zone). Market-moving (medium importance and up) only unless
    ``all=true``."""
    try:
        zone = ZoneInfo(tz) if tz else desk.local_tz
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise HTTPException(status_code=422, detail="Unknown time zone") from exc
    now = desk.clock()
    start, end = calendar_window(now + timedelta(days=7) if week == "next" else now, "week")
    rows, hidden = await desk.calendar(start, end, show_all=show_all)
    economics = getattr(request.app.state, "economics", None)
    coverage, stale = calendar_coverage(economics) if economics is not None else ([], True)
    return CalendarBoard(generated_at=now, week=week, start=start, end=end, tz=zone.key,
                         all=show_all, hidden=hidden, days=CAL.by_day(rows, zone),
                         stale=stale, coverage=coverage)


@router.get("/desk/macro", response_model=MacroBoard)
async def desk_macro(request: Request, desk: DeskDep) -> MacroBoard:
    """The Macro page: about 14 cards in four groups, each with its trend, last
    release against forecast and whether a rise is usually good or bad for NQ."""
    economics = getattr(request.app.state, "economics", None)
    series = economics.current_state().series if economics is not None else []
    return await desk.macro(series)


@router.get("/desk/news", response_model=NewsBoard)
async def desk_news(desk: DeskDep,
                    hours: Annotated[int, Query(ge=1, le=72)] = 24) -> NewsBoard:
    """Headlines from the last ``hours``, newest first, each tagged by fixed rules
    (category, scheduled or not) with NQ's move in the next 15 minutes."""
    return await desk.news(hours)


@router.get("/desk/notes", response_model=NoteList)
async def desk_notes(
    desk: DeskDep,
    start: Annotated[AwareDatetime | None, Query(alias="from")] = None,
    end: Annotated[AwareDatetime | None, Query(alias="to")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> NoteList:
    """Written notes created in ``[from, to)``, newest first. Failed and draftless notes
    are left out. Bounds need a UTC offset; ``to`` defaults to now, ``from`` to 7 days
    before ``to``."""
    end = (end if end is not None else desk.clock()).astimezone(UTC)
    start = start.astimezone(UTC) if start is not None else end - NOTES_WINDOW
    if start >= end:
        raise HTTPException(status_code=422, detail="from must be before to; ranges are half-open")
    found = desk.store.readable(start, end, limit + 1)
    notes = [NoteSummary(id=n.id, kind=n.kind, at=n.created_at, headline=n.draft.headline,
                         summary=n.draft.summary, tone=n.draft.nq_tone)
             for n in found[:limit] if n.draft is not None]
    return NoteList(start=start, end=end, limit=limit, truncated=len(found) > limit, notes=notes)


@router.get("/desk/notes/{note_id}", response_model=DeskNote)
async def desk_note(note_id: str, desk: DeskDep) -> DeskNote:
    note = desk.store.get(note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")
    return note


@router.post("/desk/ask", response_model=DeskNote)
async def desk_ask(
    desk: DeskDep,
    instrument: Annotated[str, Query(max_length=10)] = LEGACY,
) -> DeskNote:
    """Explain ``instrument`` (default NQ) right now. 400 for an instrument the
    desk does not write for (``desk_instruments``), before anything is spent."""
    try:
        inst = Instrument.of(instrument)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if inst not in desk.instruments:
        raise HTTPException(status_code=400,
                            detail=f"The desk does not write for {inst.symbol}")
    if not desk.writer.ready:
        raise HTTPException(status_code=503, detail=desk.writer.detail)
    try:
        return await desk.ask(inst)
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
