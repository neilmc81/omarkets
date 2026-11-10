"""SQLite storage for desk notes. One row per note id; ids are idempotency keys."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from core.desk.identity import INSTRUMENT_KINDS, instrument_of
from core.desk.models import (
    BreakingAlert,
    DeskNote,
    EventJoin,
    FollowUp,
    LearnEntry,
    NoteRating,
    RatingState,
)

logger = logging.getLogger(__name__)


class DeskStore:
    def __init__(self, path: Path | str) -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self._path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            # Not allowed inside a transaction; a no-op for ":memory:".
            self._db.execute("PRAGMA journal_mode=WAL")
            # One transaction for the whole schema step: SQLite DDL is
            # transactional, so a new column and its repair land together or not
            # at all, and IMMEDIATE serialises two processes opening one file.
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._migrate()
                self._db.commit()
            except BaseException:
                self._db.rollback()
                self._db.close()
                raise

    def _migrate(self) -> None:
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS desk_notes ("
            " id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,"
            " created_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 1,"
            " payload TEXT NOT NULL)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS desk_notes_created ON desk_notes(created_at)"
        )
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(desk_notes)")}
        if "failure_alerted" not in columns:
            self._db.execute(
                "ALTER TABLE desk_notes ADD COLUMN failure_alerted INTEGER NOT NULL DEFAULT 0"
            )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS desk_alerts ("
            " id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS desk_once ("
            " id TEXT PRIMARY KEY, created_at TEXT NOT NULL)"
        )
        if "followup" not in columns:
            # "Did it hold?" results; kept apart so a written note is never rewritten.
            self._db.execute("ALTER TABLE desk_notes ADD COLUMN followup TEXT")
        if "instrument" not in columns:
            # Which instrument a note is about (None for desk-wide event notes).
            self._db.execute("ALTER TABLE desk_notes ADD COLUMN instrument TEXT")
        self._repair_instruments()
        # Instruments that joined a written move note later (desk:v11). Apart from the
        # note so a written note is never modified; one row per (note, instrument).
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS desk_event_joins ("
            " note_id TEXT NOT NULL, instrument TEXT NOT NULL, joined_at TEXT NOT NULL,"
            " move_json TEXT NOT NULL, PRIMARY KEY (note_id, instrument))"
        )
        # The exact facts a note was written from, and how it was requested.
        # Kept out of the note payload so older releases still read the notes.
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS note_facts ("
            " note_id TEXT PRIMARY KEY, facts TEXT NOT NULL, envelope TEXT NOT NULL,"
            " captured_at TEXT NOT NULL)"
        )

        # The reader's useful / not useful mark, apart from the note so a written note
        # is never modified. One row per note; clearing a mark deletes its row.
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS desk_ratings ("
            " note_id TEXT PRIMARY KEY,"
            " rating TEXT NOT NULL CHECK (rating IN ('useful', 'not_useful')),"
            " rated_at TEXT NOT NULL)"
        )

    def _repair_instruments(self) -> None:
        """Fill in the instrument of every pre-market, move and ask row that has
        none, from its id, as ``save`` would. Runs on every open: it also mends
        rows written by an older release that does not know the column."""
        kinds = sorted(k.value for k in INSTRUMENT_KINDS)
        rows = self._db.execute(
            "SELECT id FROM desk_notes WHERE instrument IS NULL"
            f" AND kind IN ({','.join('?' * len(kinds))})", kinds,
        ).fetchall()
        repairs = [(symbol, note_id) for (note_id,) in rows
                   if (symbol := instrument_of(note_id)) is not None]
        if repairs:
            self._db.executemany("UPDATE desk_notes SET instrument=? WHERE id=?", repairs)
            logger.info("desk store: set the instrument of %d older note(s)", len(repairs))

    @staticmethod
    def _note(payload: str, followup: str | None, instrument: str | None = None) -> DeskNote:
        note = DeskNote.model_validate_json(payload)
        update: dict[str, Any] = {"instrument": instrument}
        if followup:
            update["follow_up"] = FollowUp.model_validate_json(followup)
        return note.model_copy(update=update)

    def _attach(self, notes: list[DeskNote]) -> list[DeskNote]:
        """``notes`` with everything stored apart from them: later joins and the mark."""
        return self._with_ratings(self._with_joins(notes))

    def _with_ratings(self, notes: list[DeskNote]) -> list[DeskNote]:
        if not notes:
            return notes
        with self._lock:
            rows = self._db.execute(
                "SELECT note_id, rating, rated_at FROM desk_ratings"
                " WHERE note_id IN (SELECT value FROM json_each(?))",
                (json.dumps([n.id for n in notes]),),
            ).fetchall()
        marks = {note_id: (rating, rated_at) for note_id, rating, rated_at in rows}
        return [n.model_copy(update={"rating": marks[n.id][0],
                                     "rated_at": datetime.fromisoformat(marks[n.id][1])})
                if n.id in marks else n for n in notes]

    def set_rating(self, note_id: str, rating: NoteRating, at: datetime) -> RatingState | None:
        """Mark a written note, replacing any earlier mark. ``None`` when there is no
        written note with that id (a failed note cannot be rated)."""
        stamp = at.astimezone(UTC)
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT INTO desk_ratings (note_id, rating, rated_at)"
                " SELECT ?,?,? WHERE EXISTS (SELECT 1 FROM desk_notes WHERE id=? AND status='ok')"
                " ON CONFLICT(note_id) DO UPDATE SET rating=excluded.rating,"
                " rated_at=excluded.rated_at",
                (note_id, rating, stamp.isoformat(), note_id),
            )
        if cursor.rowcount != 1:
            return None
        return RatingState(note_id=note_id, rating=rating, rated_at=stamp)

    def clear_rating(self, note_id: str) -> bool:
        """Remove a note's mark. True when the note exists (it may already have been
        unrated); False when there is no such note."""
        with self._lock, self._db:
            self._db.execute("DELETE FROM desk_ratings WHERE note_id=?", (note_id,))
            row = self._db.execute("SELECT 1 FROM desk_notes WHERE id=?", (note_id,)).fetchone()
        return row is not None

    def between(self, start: datetime, end: datetime) -> list[DeskNote]:
        """Every note, written or failed, created in ``[start, end)``, oldest first,
        with its follow-up and mark. For the scorecard."""
        with self._lock:
            rows = self._db.execute(
                "SELECT payload, followup, instrument FROM desk_notes"
                " WHERE created_at>=? AND created_at<? ORDER BY created_at, id",
                (start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat()),
            ).fetchall()
        return self._notes(rows)

    def _with_joins(self, notes: list[DeskNote]) -> list[DeskNote]:
        """``notes`` with the instruments that joined each one later."""
        ids = [n.id for n in notes if n.kind.value == "move"]
        if not ids:
            return notes
        with self._lock:
            rows = self._db.execute(
                "SELECT note_id, instrument, joined_at, move_json FROM desk_event_joins"
                " WHERE note_id IN (SELECT value FROM json_each(?))"
                " ORDER BY joined_at, instrument",
                (json.dumps(ids),),
            ).fetchall()
        if not rows:
            return notes
        joins: dict[str, list[EventJoin]] = {}
        for note_id, instrument, joined_at, move_json in rows:
            try:
                joins.setdefault(note_id, []).append(EventJoin.model_validate(
                    {"note_id": note_id, "instrument": instrument, "joined_at": joined_at,
                     "move": json.loads(move_json)}))
            except (ValidationError, ValueError):
                logger.error("desk join %s/%s unreadable, skipped", note_id, instrument)
        return [n.model_copy(update={"event_joins": joins[n.id]}) if n.id in joins else n
                for n in notes]

    def add_join(self, join: EventJoin) -> bool:
        """Record that an instrument joined a written move note. False if it already
        had (one row per note and instrument), or the note is not a written one."""
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO desk_event_joins (note_id, instrument, joined_at,"
                " move_json) SELECT ?,?,?,? WHERE EXISTS ("
                "SELECT 1 FROM desk_notes WHERE id=? AND status='ok')",
                (join.note_id, join.instrument, join.joined_at.isoformat(),
                 join.move.model_dump_json(), join.note_id),
            )
        return cursor.rowcount == 1

    def moves_since(self, since_iso: str) -> list[DeskNote]:
        """Written move notes created since then, newest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT payload, followup, instrument FROM desk_notes WHERE kind='move'"
                " AND status='ok' AND created_at>=? ORDER BY created_at DESC, id",
                (since_iso,),
            ).fetchall()
        return self._notes(rows)

    def _notes(self, rows: list[tuple[str, str | None, str | None]]) -> list[DeskNote]:
        """Readable notes from ``rows``; one unreadable payload is logged and skipped,
        so it can never blank a whole list."""
        notes = []
        for payload, followup, instrument in rows:
            try:
                notes.append(self._note(payload, followup, instrument))
            except ValidationError as exc:
                found = re.search(r'"id":\s*"([^"]*)"', payload)
                logger.error("desk note %s unreadable, skipped: %d errors",
                             found.group(1) if found else "?", exc.error_count())
        return self._attach(notes)

    def get(self, note_id: str) -> DeskNote | None:
        with self._lock:
            row = self._db.execute(
                "SELECT payload, followup, instrument FROM desk_notes WHERE id=?", (note_id,)
            ).fetchone()
        return self._attach([self._note(*row)])[0] if row else None

    def attempts(self, note_id: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT attempts FROM desk_notes WHERE id=?", (note_id,)
            ).fetchone()
        return int(row[0]) if row else 0

    def has_ok(self, note_id: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM desk_notes WHERE id=? AND status='ok'", (note_id,)
            ).fetchone()
        return row is not None

    def save(self, note: DeskNote, facts: dict[str, Any] | None = None,
             envelope: dict[str, Any] | None = None) -> None:
        """Insert, or replace a failed attempt. A successful note is never overwritten,
        and nor are the facts it was written from."""
        captured = None
        if facts is not None:
            # Serialised before the transaction: a facts problem must never lose
            # an already-paid note.
            try:
                captured = (json.dumps(facts, default=str), json.dumps(envelope or {}))
            except (TypeError, ValueError):
                logger.exception("desk note %s: facts not serialisable, not captured", note.id)
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT INTO desk_notes (id, kind, status, created_at, payload, instrument)"
                " VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET status=excluded.status,"
                " created_at=excluded.created_at, payload=excluded.payload,"
                " attempts=desk_notes.attempts+1"
                " WHERE desk_notes.status != 'ok'",
                (note.id, note.kind.value, note.status, note.created_at.isoformat(),
                 note.model_dump_json(exclude={"follow_up", "instrument", "tone_for",
                                               "event_instruments", "rating", "rated_at"}),
                 instrument_of(note.id)),
            )
            if cursor.rowcount == 1 and captured is not None:
                self._db.execute(
                    "INSERT OR REPLACE INTO note_facts (note_id, facts, envelope, captured_at)"
                    " VALUES (?,?,?,?)",
                    (note.id, *captured, note.created_at.isoformat()),
                )

    def facts(self, note_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """The facts and request envelope a note was written from, if captured."""
        with self._lock:
            row = self._db.execute(
                "SELECT facts, envelope FROM note_facts WHERE note_id=?", (note_id,)
            ).fetchone()
        return (json.loads(row[0]), json.loads(row[1])) if row else None

    def backup(self, target: Path | str) -> None:
        """A consistent copy of the whole database (WAL included), via SQLite's backup."""
        out = sqlite3.connect(str(target))
        try:
            with self._lock:
                self._db.backup(out)
        finally:
            out.close()

    def save_follow_up(self, note_id: str, follow_up: FollowUp) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE desk_notes SET followup=? WHERE id=? AND status='ok'",
                (follow_up.model_dump_json(), note_id),
            )

    def claim_failure_alert(self, note_id: str) -> bool:
        """Mark a failed note as alerted. True only the first time, so one alert per id."""
        with self._lock, self._db:
            cursor = self._db.execute(
                "UPDATE desk_notes SET failure_alerted=1"
                " WHERE id=? AND status='failed' AND failure_alerted=0",
                (note_id,),
            )
        return cursor.rowcount == 1

    def claim_once(self, key: str, at: datetime) -> bool:
        """True only the first time ``key`` is claimed (e.g. one daily plan per day)."""
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO desk_once (id, created_at) VALUES (?,?)",
                (key, at.isoformat()),
            )
        return cursor.rowcount == 1

    def save_alert(self, alert: BreakingAlert) -> bool:
        """Record an alert. False if this headline already alerted (one alert per id)."""
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO desk_alerts (id, created_at, payload) VALUES (?,?,?)",
                (alert.id, alert.created_at.isoformat(), alert.model_dump_json()),
            )
        return cursor.rowcount == 1

    def has_alert(self, alert_id: str) -> bool:
        with self._lock:
            row = self._db.execute("SELECT 1 FROM desk_alerts WHERE id=?", (alert_id,)).fetchone()
        return row is not None

    def alerts_since(self, since: datetime) -> list[BreakingAlert]:
        with self._lock:
            rows = self._db.execute(
                "SELECT payload FROM desk_alerts WHERE created_at>=? ORDER BY created_at DESC",
                (since.isoformat(),),
            ).fetchall()
        return [BreakingAlert.model_validate_json(row[0]) for row in rows]

    def recent(self, limit: int = 30, *, include_failed: bool = False) -> list[DeskNote]:
        clause = "" if include_failed else "WHERE status='ok'"
        with self._lock:
            rows = self._db.execute(
                f"SELECT payload, followup, instrument FROM desk_notes {clause}"
                " ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return self._notes(rows)

    def readable(self, start: datetime, end: datetime, limit: int,
                 instrument: str | None = None) -> list[DeskNote]:
        """Written notes (status ok, with a draft) created in ``[start, end)``, newest first.

        With ``instrument``: that instrument's own notes, the cross-market move
        notes that list it among their members, and the desk-wide notes."""
        # created_at is stored as UTC ISO text, so UTC bounds compare as text.
        with self._lock:
            rows = self._db.execute(
                "SELECT payload, followup, instrument FROM desk_notes WHERE status='ok'"
                " AND json_valid(payload) AND json_extract(payload, '$.draft') IS NOT NULL"
                " AND created_at>=? AND created_at<?"
                " AND (? IS NULL OR instrument IS NULL OR instrument=? OR EXISTS ("
                "SELECT 1 FROM json_each(desk_notes.payload, '$.event_members') WHERE value=?)"
                " OR EXISTS (SELECT 1 FROM desk_event_joins j WHERE j.note_id=desk_notes.id"
                " AND j.instrument=?))"
                " ORDER BY created_at DESC, id LIMIT ?",
                (start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat(),
                 instrument, instrument, instrument, instrument, limit),
            ).fetchall()
        return self._notes(rows)

    def release_note_ids(self) -> list[str]:
        """Ids of written release and Fed notes, for linking calendar rows to them."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id FROM desk_notes WHERE status='ok' AND id LIKE 'release:%'"
                " AND json_valid(payload) AND json_extract(payload, '$.draft') IS NOT NULL"
            ).fetchall()
        return [row[0] for row in rows]

    def learn_entries(self) -> list[LearnEntry]:
        """Every written note's Learn box, newest first, with repeats merged.

        Two lessons are the same when they match after ignoring case, spacing
        and punctuation.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT payload FROM desk_notes WHERE status='ok' ORDER BY created_at DESC"
            ).fetchall()
        merged: dict[str, LearnEntry] = {}
        for note in self._notes([(payload, None, None) for (payload,) in rows]):
            text = " ".join((note.draft.learn if note.draft else "").split())
            key = re.sub(r"[^\w ]", "", text.casefold()).strip()
            if not key:
                continue
            if key in merged:
                entry = merged[key]
                merged[key] = entry.model_copy(update={"times": entry.times + 1})
                continue
            merged[key] = LearnEntry(text=text, note_id=note.id, kind=note.kind,
                                     title=note.title, created_at=note.created_at, times=1)
        return list(merged.values())

    def latest_ok(self, kind: str, since_iso: str,
                  instrument: str | None = None) -> DeskNote | None:
        """The newest written note of ``kind`` since then; for one instrument if given."""
        with self._lock:
            row = self._db.execute(
                "SELECT payload, followup, instrument FROM desk_notes WHERE kind=? AND status='ok'"
                " AND created_at>=? AND (? IS NULL OR instrument=?)"
                " ORDER BY created_at DESC LIMIT 1",
                (kind, since_iso, instrument, instrument),
            ).fetchone()
        return self._attach([self._note(*row)])[0] if row else None

    def count_since(self, kind: str, since_iso: str, instrument: str | None = None,
                    *, written_only: bool = False, with_members: bool = False) -> int:
        """Notes of ``kind`` since then (any status unless ``written_only``);
        for one instrument if given. ``with_members`` also counts a cross-market
        move note that lists the instrument among its ``event_members``."""
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM desk_notes WHERE kind=? AND created_at>=?"
                " AND (? IS NULL OR instrument=? OR (? = 1 AND json_valid(payload) AND EXISTS ("
                "SELECT 1 FROM json_each(desk_notes.payload, '$.event_members') WHERE value=?)))"
                " AND (? = 0 OR status='ok')",
                (kind, since_iso, instrument, instrument, int(with_members), instrument,
                 int(written_only)),
            ).fetchone()
            joined = self._db.execute(
                "SELECT COUNT(*) FROM desk_event_joins j JOIN desk_notes n ON n.id=j.note_id"
                " WHERE ? = 1 AND ? IS NOT NULL AND j.instrument=? AND j.joined_at>=?"
                " AND n.kind=?",
                (int(with_members), instrument, instrument, since_iso, kind),
            ).fetchone()
        return int(row[0]) + int(joined[0])

    def close(self) -> None:
        with self._lock:
            self._db.close()
