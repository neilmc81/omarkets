"""Evaluation cases and their frozen manifest.

A case is one stored note: the facts it was written from, how it was requested,
and the draft that was written (the baseline). ``capture`` copies cases out of a
desk database without writing to it; ``freeze`` records a SHA-256 for every
case file; every later command loads cases through ``load`` and refuses a
manifest whose files changed, went missing or gained a stranger.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MANIFEST = "manifest.json"
HISTORY = "lesson_history.json"
CASES = "cases"
#: Facts kept with every note before full capture (Phase 1 slice 1).
PARTIAL_FACT_KEYS = ("figures", "moves", "headlines", "upcoming", "rates", "event_at")


class ManifestError(RuntimeError):
    pass


@dataclass(frozen=True)
class Case:
    id: str
    kind: str
    title: str
    created_at: str
    facts: dict[str, Any]
    #: True when ``facts`` is everything the writer saw (captured with the note).
    facts_complete: bool
    #: How the note was requested (prompt version, hashes, model, routing), if captured.
    envelope: dict[str, Any] | None
    #: The note as stored: ``{"prompt_version", "model", "draft"}``.
    baseline: dict[str, Any]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def file_name(case_id: str) -> str:
    return re.sub(r"[^\w.-]", "_", case_id) + ".json"


def _readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"no desk database at {db_path}")
    return sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)


def capture(db_path: Path, note_ids: Sequence[str]) -> tuple[list[Case], list[dict[str, Any]]]:
    """Cases for ``note_ids`` and every stored Learn box, read without writing."""
    db = _readonly(db_path)
    try:
        has_facts = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                               "AND name='note_facts'").fetchone() is not None
        cases = []
        for note_id in note_ids:
            row = db.execute("SELECT kind, created_at, payload FROM desk_notes "
                             "WHERE id=? AND status='ok'", (note_id,)).fetchone()
            if row is None:
                raise KeyError(f"no written note {note_id}")
            kind, created_at, payload = row[0], row[1], json.loads(row[2])
            if payload.get("draft") is None:
                raise KeyError(f"note {note_id} has no draft")
            captured = has_facts and db.execute(
                "SELECT facts, envelope FROM note_facts WHERE note_id=?", (note_id,)).fetchone()
            if captured:
                facts, envelope = json.loads(captured[0]), json.loads(captured[1])
            else:
                facts = {key: payload.get(key) for key in PARTIAL_FACT_KEYS}
                envelope = None
            model, _, version = str(payload.get("model") or "").partition(" · ")
            cases.append(Case(
                id=note_id, kind=kind, title=payload.get("title") or "", created_at=created_at,
                facts=facts, facts_complete=bool(captured), envelope=envelope,
                baseline={"prompt_version": version or None, "model": model,
                          "draft": payload["draft"]},
            ))
        history = [
            {"id": note_id, "kind": kind, "created_at": created_at,
             "learn": json.loads(payload)["draft"].get("learn")}
            for note_id, kind, created_at, payload in db.execute(
                "SELECT id, kind, created_at, payload FROM desk_notes WHERE status='ok' "
                "AND json_extract(payload, '$.draft') IS NOT NULL ORDER BY created_at")
        ]
    finally:
        db.close()
    return cases, history


def write_cases(directory: Path, cases: Sequence[Case], history: Sequence[dict]) -> None:
    """Write case files and the lesson history. Refuses to touch a frozen set."""
    if (directory / MANIFEST).exists():
        raise ManifestError(f"{directory} is frozen; capture into a new folder")
    (directory / CASES).mkdir(parents=True, exist_ok=True)
    for case in cases:
        (directory / CASES / file_name(case.id)).write_text(
            json.dumps(asdict(case), indent=1, ensure_ascii=False) + "\n")
    (directory / HISTORY).write_text(json.dumps(list(history), indent=1, ensure_ascii=False)
                                     + "\n")


def _listing(directory: Path) -> tuple[list[dict[str, Any]], dict[str, str]]:
    entries = []
    for path in sorted((directory / CASES).glob("*.json")):
        case = json.loads(path.read_text())
        entries.append({"id": case["id"], "kind": case["kind"],
                        "file": f"{CASES}/{path.name}", "sha256": _sha(path.read_bytes()),
                        "facts_complete": case["facts_complete"]})
    history = {"file": HISTORY, "sha256": _sha((directory / HISTORY).read_bytes())}
    return entries, history


def freeze(directory: Path, name: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Record every case file's hash. A frozen manifest is never rewritten."""
    path = directory / MANIFEST
    if path.exists():
        raise ManifestError(f"{path} already exists; frozen cases are never re-frozen")
    entries, history = _listing(directory)
    if not entries:
        raise ManifestError(f"no case files in {directory / CASES}")
    body = {"name": name, "cases": entries, "lesson_history": history}
    manifest = {**body, "frozen_at": (now or datetime.now(UTC)).isoformat(),
                "sha256": _sha(_canonical(body))}
    path.write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n")
    return manifest


@dataclass(frozen=True)
class CaseSet:
    directory: Path
    name: str
    #: Identifies this exact set of cases; results and runs are tied to it.
    sha256: str
    cases: list[Case]
    history: list[dict[str, Any]]

    def case(self, case_id: str) -> Case:
        for case in self.cases:
            if case.id == case_id:
                return case
        raise KeyError(case_id)


def load(directory: Path) -> CaseSet:
    """The frozen cases, verified against the manifest; any difference refuses."""
    path = directory / MANIFEST
    if not path.exists():
        raise ManifestError(f"{path} not found; freeze the cases first")
    manifest = json.loads(path.read_text())
    body = {key: manifest.get(key) for key in ("name", "cases", "lesson_history")}
    if _sha(_canonical(body)) != manifest.get("sha256"):
        raise ManifestError(f"{path} was edited after freezing")
    try:
        entries, history = _listing(directory)
    except FileNotFoundError as exc:
        raise ManifestError(f"missing file: {exc.filename}") from exc
    if entries != manifest["cases"] or history != manifest["lesson_history"]:
        recorded = {e["file"]: e["sha256"] for e in manifest["cases"]}
        actual = {e["file"]: e["sha256"] for e in entries}
        changed = sorted(f for f in recorded.keys() | actual.keys()
                         if recorded.get(f) != actual.get(f))
        if history != manifest["lesson_history"]:
            changed.append(HISTORY)
        raise ManifestError(f"cases differ from the frozen manifest: {', '.join(changed)}")
    cases = [Case(**json.loads((directory / e["file"]).read_text())) for e in entries]
    return CaseSet(directory=directory, name=manifest["name"], sha256=manifest["sha256"],
                   cases=cases, history=json.loads((directory / HISTORY).read_text()))
