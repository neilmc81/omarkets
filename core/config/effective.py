"""What the running service is really configured with, and where each value came from.

Settings come from four places (code defaults, the project ``.env``, systemd
``Environment=``/``EnvironmentFile=`` and legacy ``MARKETOS_*`` names), and docs
can drift from all of them. This module rebuilds ``Settings`` from the live
service's own environment so a reader can quote the real values. Secret values
are never returned: only whether each one is set.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values
from pydantic import AliasChoices, SecretStr

from core.config.settings import LEGACY_ENV_PREFIX, Settings
from core.desk.models import NoteKind

ENV_PREFIX = "OMARKETS_"
SERVICE_UNIT = "omarkets.service"


@dataclass(frozen=True)
class SettingSource:
    name: str
    value: str
    #: "default", "service env (NAME)" or ".env (NAME)".
    source: str


@dataclass(frozen=True)
class NoteCostRow:
    #: Model and prompt version as stored, e.g. "deepseek/... · desk:v4".
    setup: str
    notes: int
    total_usd: float
    first_at: str
    last_at: str

    @property
    def mean_usd(self) -> float:
        return self.total_usd / self.notes if self.notes else 0.0


def _main_pid(unit: str) -> str | None:
    try:
        pid = subprocess.run(
            ["systemctl", "--user", "show", "-p", "MainPID", "--value", unit],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return pid if pid and pid != "0" else None


def service_process(unit: str = SERVICE_UNIT) -> tuple[dict[str, str], Path] | None:
    """The environment and working directory of the unit's running main process.

    ``None`` when the unit is not running or its process cannot be read.
    """
    pid = _main_pid(unit)
    if pid is None:
        return None
    try:
        cwd = Path(os.readlink(f"/proc/{pid}/cwd"))
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    items = raw.decode("utf-8", "replace").split("\0")
    return dict(item.split("=", 1) for item in items if "=" in item), cwd


@contextmanager
def _environment(environ: Mapping[str, str]) -> Iterator[None]:
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _env_names(name: str, alias: object) -> list[str]:
    if isinstance(alias, AliasChoices):
        return [str(choice) for choice in alias.choices]
    upper = name.upper()
    return [ENV_PREFIX + upper, LEGACY_ENV_PREFIX + upper]


def _source(names: list[str], environ: Mapping[str, str], dotenv: Mapping[str, str | None]) -> str:
    # Same precedence as Settings: each name in the process env, then in .env.
    for name in names:
        if name in environ:
            return f"service env ({name})"
        if dotenv.get(name) not in (None, ""):
            return f".env ({name})"
    return "default"


def _render(value: object) -> str:
    if isinstance(value, dict):
        return json.dumps({str(k): v for k, v in value.items()}, default=str)
    if isinstance(value, list | tuple):
        return json.dumps(list(value), default=str)
    return str(value)


def effective_settings(
    environ: Mapping[str, str], env_file: Path | None
) -> tuple[Settings, list[SettingSource]]:
    """Build ``Settings`` exactly as a process with ``environ`` and ``env_file`` would."""
    dotenv = dotenv_values(env_file) if env_file is not None and env_file.exists() else {}
    with _environment(environ):
        settings = Settings(_env_file=env_file)
    rows: list[SettingSource] = []
    for name, field in Settings.model_fields.items():
        value = getattr(settings, name)
        secret = (
            isinstance(value, SecretStr)
            or "secret" in str(field.annotation).lower()
            or any(word in name for word in ("key", "token", "secret", "password"))
        )
        shown = ("set" if value else "not set") if secret else _render(value)
        names = _env_names(name, field.validation_alias)
        rows.append(SettingSource(name, shown, _source(names, environ, dotenv)))
    return settings, rows


def desk_routing(settings: Settings) -> dict[str, str]:
    """The model that writes each note kind, plus the retry model."""
    routing = {kind.value: settings.desk_model_for(kind) for kind in NoteKind}
    routing["(fallback)"] = settings.desk_fallback_model or "none"
    return routing


def note_costs(db_path: Path) -> list[NoteCostRow]:
    """Stored desk-note costs grouped by model and prompt version. Read-only."""
    if not db_path.exists():
        return []
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = db.execute(
            "SELECT created_at, payload FROM desk_notes ORDER BY created_at"
        ).fetchall()
    finally:
        db.close()
    groups: dict[str, list[tuple[str, float]]] = {}
    for created_at, payload in rows:
        note = json.loads(payload)
        setup = note.get("model") or "unknown"
        groups.setdefault(setup, []).append((created_at, float(note.get("cost_usd") or 0.0)))
    return [
        NoteCostRow(setup, len(items), sum(cost for _, cost in items), items[0][0], items[-1][0])
        for setup, items in sorted(groups.items(), key=lambda pair: pair[1][0][0])
    ]
