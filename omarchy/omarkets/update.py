#!/usr/bin/env python3
"""Omarkets bar data collector.

Fetches the compact, versioned shell status from the local Omarkets API and
writes it atomically to ``$XDG_STATE_HOME/omarkets/bar.json``. The QML bar widget
and popup read that one file, so the UI holds no market logic and never talks to
a provider or assembles several backend models.

Design notes:

* The API is loopback-only and unauthenticated; the URL is fixed and validated.
* The core does all normalization and formatting (including timezone display
  strings), so this script copies the payload through verbatim after validating
  its shape and version.
* Responses are parsed as data only; nothing from a response is executed.
* On failure the previous good status and its timestamp are retained while
  transport failure metadata is written, so the shell can retry without
  presenting old market data as current.

Stdlib only.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime

_ASCII_DIGITS = re.compile(r"[0-9]+")

HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME", HOME + "/.local/state"), "omarkets")
OUT = os.path.join(STATE_DIR, "bar.json")
# Startup ingestion can briefly delay an API response; the collector must be
# generous enough not to flap, while still failing fast when truly unreachable.
TIMEOUT = 8

#: The shell contract version this collector understands. A newer core payload
#: is still passed through (forward compatible) but a mismatch is recorded so a
#: stale shell is visible rather than silently mis-rendering.
SUPPORTED_VERSION = 2


def get(base: str, path: str) -> tuple[object | None, str | None]:
    """Fetch one JSON endpoint. Returns ``(data, error)``."""
    try:
        request = urllib.request.Request(base + path, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8")), None
    except urllib.error.HTTPError as exc:
        return None, f"http {exc.code}"
    except urllib.error.URLError as exc:
        return None, f"unreachable: {exc.reason}"
    except (ValueError, TimeoutError, OSError) as exc:
        # JSON decode errors, timeouts, and mid-read socket faults.
        return None, type(exc).__name__


def desk_summary(desk: object) -> dict | None:
    """The latest plain-English desk note, reduced to what the popup shows."""
    if not isinstance(desk, dict):
        return None
    notes = desk.get("notes")
    note = notes[0] if isinstance(notes, list) and notes and isinstance(notes[0], dict) else None
    draft = note.get("draft") if note else None
    summary = None
    if note and isinstance(draft, dict):
        fields = {
            "id": note.get("id"), "kind": note.get("kind"), "title": note.get("title"),
            "created_at": note.get("created_at"), "headline": draft.get("headline"),
            "summary": draft.get("summary"), "tone": draft.get("nq_tone") or draft.get("tone"),
        }
        if all(isinstance(value, str) for value in fields.values()):
            summary = fields
    session = desk.get("session_text")
    return {"session_text": session if isinstance(session, str) else None, "note": summary}


THEME_ROLES = ("green", "red", "orange", "blue", "accent", "foreground", "background")
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def theme_summary(theme: object) -> dict | None:
    """The active Omarchy palette roles the popup colours its states with."""
    if not isinstance(theme, dict) or not isinstance(theme.get("colors"), dict):
        return None
    colors = {role: value for role in THEME_ROLES
              if isinstance(value := theme["colors"].get(role), str) and _HEX.match(value)}
    version = theme.get("version")
    return {"version": version if isinstance(version, str) else None, "colors": colors}


def collect(base: str) -> dict:
    """Fetch and shape the shell payload. Copies core values verbatim.

    The status endpoint is required; the context endpoint is optional (used by
    the popup for the wider cross-asset strip) and its failure does not fail the
    whole collection.
    """
    status, status_err = get(base, "/api/v1/shell/status")
    errors = {"status": status_err}
    if not isinstance(status, dict):
        # Record the failure but keep any previous good file on disk.
        raise RuntimeError(status_err or "invalid shell status payload")

    context, context_err = get(base, "/api/v1/shell/context")
    errors["context"] = context_err

    desk, desk_err = get(base, "/api/v1/desk?limit=1")
    errors["desk"] = desk_err

    theme, theme_err = get(base, "/api/v1/theme")
    errors["theme"] = theme_err

    version = status.get("version")
    if version != SUPPORTED_VERSION:
        errors["version"] = f"core={version} shell={SUPPORTED_VERSION}"

    collected_at = datetime.now(UTC).isoformat()
    return {
        "version": SUPPORTED_VERSION,
        "updatedAt": collected_at,
        "attemptedAt": collected_at,
        "consecutiveFailures": 0,
        "error": "partial" if any(v for v in errors.values()) else None,
        "errors": errors,
        "status": status,
        "context": context if isinstance(context, dict) else None,
        "desk": desk_summary(desk),
        "theme": theme_summary(theme),
    }


def write_atomic(payload: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = f"{OUT}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"))
    os.replace(tmp, OUT)


def record_failure() -> None:
    """Keep the last valid status and report a failed collection to the UI."""
    try:
        with open(OUT, encoding="utf-8") as handle:
            previous = json.load(handle)
    except (OSError, ValueError):
        previous = {}
    if not isinstance(previous, dict):
        previous = {}
    previous = dict(previous)
    previous["attemptedAt"] = datetime.now(UTC).isoformat()
    try:
        count = max(0, int(previous.get("consecutiveFailures") or 0))
    except (TypeError, ValueError):
        count = 0
    previous["consecutiveFailures"] = count + 1
    previous["error"] = "transport"
    previous["errors"] = {"status": "collector failed"}
    write_atomic(previous)


def main(argv: list[str]) -> int:
    port = 8765
    if len(argv) > 1:
        text = argv[1].strip()
        # ``str.isdigit`` accepts non-ASCII digits that ``int`` rejects, so match
        # ASCII digits explicitly.
        if not _ASCII_DIGITS.fullmatch(text) or not 1 <= int(text) <= 65535:
            print("update.py: port must be 1..65535", file=sys.stderr)
            return 2
        port = int(text)
    base = f"http://127.0.0.1:{port}"
    try:
        payload = collect(base)
    except Exception as exc:  # pragma: no cover - defensive; retain last good status
        record_failure()
        print(f"update.py: collection failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    write_atomic(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
