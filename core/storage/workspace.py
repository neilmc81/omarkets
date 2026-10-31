"""SQLite storage for the Chart focus and the watchlist.

A file of its own (``workspace.sqlite3``), apart from the market database and
the desk notes, so saving a choice on the Chart page can never touch either.
One row per setting; a row that no longer validates (e.g. a symbol dropped from
the registry) reads as "not set", so the caller's default applies.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from pydantic import AwareDatetime, TypeAdapter, ValidationError

from core.domain.workspace import FocusUpdate, Watchlist

logger = logging.getLogger(__name__)

_AT = TypeAdapter(AwareDatetime)

FOCUS_KEY = "chart_focus"
WATCHLIST_KEY = "watchlist"


class WorkspaceStore:
    def __init__(self, path: Path | str) -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self._path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS workspace ("
                " key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _get(self, key: str) -> tuple[str, str] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT value, updated_at FROM workspace WHERE key=?", (key,)).fetchone()
        return None if row is None else (row[0], row[1])

    def _put(self, key: str, value: str, now: datetime | None) -> datetime:
        at = (now or datetime.now(UTC)).astimezone(UTC)
        with self._lock:
            self._db.execute(
                "INSERT INTO workspace (key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                " updated_at=excluded.updated_at",
                (key, value, at.isoformat()),
            )
            self._db.commit()
        return at

    def focus(self) -> tuple[str, datetime] | None:
        row = self._get(FOCUS_KEY)
        if row is None:
            return None
        try:
            return FocusUpdate.model_validate_json(row[0]).symbol, _AT.validate_python(row[1])
        except (ValidationError, ValueError):
            logger.warning("workspace: stored chart focus no longer valid; using the default")
            return None

    def set_focus(self, focus: FocusUpdate, *, now: datetime | None = None) -> datetime:
        return self._put(FOCUS_KEY, focus.model_dump_json(), now)

    def watchlist(self) -> tuple[Watchlist, datetime] | None:
        row = self._get(WATCHLIST_KEY)
        if row is None:
            return None
        try:
            return Watchlist.model_validate_json(row[0]), _AT.validate_python(row[1])
        except (ValidationError, ValueError):
            logger.warning("workspace: stored watchlist no longer valid; using the default")
            return None

    def set_watchlist(self, watchlist: Watchlist, *, now: datetime | None = None) -> datetime:
        return self._put(WATCHLIST_KEY, watchlist.model_dump_json(), now)


__all__ = ["WorkspaceStore"]
