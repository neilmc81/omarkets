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
from core.desk.models import BreakingAlert, DeskNote, FollowUp, LearnEntry

logger = logging.getLogger(__name__)


class DeskStore:
    def __init__(self, path: Path | str) -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self._path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.execute("PRAGMA journal_mode=WAL")
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
                kinds = ",".join(f"'{k.value}'" for k in INSTRUMENT_KINDS)
                # Every note written before this column is NQ's.
                self._db.execute(
                    f"UPDATE desk_notes SET instrument='NQ' WHERE kind IN ({kinds})")
            # The exact facts a note was written from, and how it was requested.
            # Kept out of the note payload so older releases still read the notes.
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS note_facts ("
                " note_id TEXT PRIMARY KEY, facts TEXT NOT NULL, envelope TEXT NOT NULL,"
                " captured_at TEXT NOT NULL)"
            )

    @staticmethod
    def _note(payload: str, followup: str | None, instrument: str | None = None) -> DeskNote:
        note = DeskNote.model_validate_json(payload)
        update: dict[str, Any] = {"instrument": instrument}
        if followup:
            update["follow_up"] = FollowUp.model_validate_json(followup)
        return note.model_copy(update=update)

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
        return notes

    def get(self, note_id: str) -> DeskNote | None:
        with self._lock:
            row = self._db.execute(
                "SELECT payload, followup, instrument FROM desk_notes WHERE id=?", (note_id,)
            ).fetchone()
        return self._note(*row) if row else None

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
                 note.model_dump_json(exclude={"follow_up", "instrument"}),
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

    def readable(self, start: datetime, end: datetime, limit: int) -> list[DeskNote]:
        """Written notes (status ok, with a draft) created in ``[start, end)``, newest first."""
        # created_at is stored as UTC ISO text, so UTC bounds compare as text.
        with self._lock:
            rows = self._db.execute(
                "SELECT payload, followup, instrument FROM desk_notes WHERE status='ok'"
                " AND json_valid(payload) AND json_extract(payload, '$.draft') IS NOT NULL"
                " AND created_at>=? AND created_at<? ORDER BY created_at DESC, id LIMIT ?",
                (start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat(), limit),
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
        return self._note(*row) if row else None

    def count_since(self, kind: str, since_iso: str, instrument: str | None = None) -> int:
        """Notes of ``kind`` (any status) since then; for one instrument if given."""
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM desk_notes WHERE kind=? AND created_at>=?"
                " AND (? IS NULL OR instrument=?)",
                (kind, since_iso, instrument, instrument),
            ).fetchone()
        return int(row[0])

    def close(self) -> None:
        with self._lock:
            self._db.close()
