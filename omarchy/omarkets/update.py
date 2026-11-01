#!/usr/bin/env python3
"""Omarkets bar data collector.

Fetches the compact, versioned shell status (and the popup's context, sparklines
and hero levels) from the local Omarkets API and writes it atomically to
``$XDG_STATE_HOME/omarkets/bar.json``. The QML bar widget
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
            # The instrument the tone is about; an older core sends only
            # ``instrument`` (None for desk-wide notes), and then none is named.
            tone_for = note.get("tone_for") or note.get("instrument")
            summary = {**fields, "tone_for": tone_for if isinstance(tone_for, str) else None}
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


#: A registry symbol, safe to put in a URL path or query.
_SYMBOL = re.compile(r"^[A-Z0-9]{1,10}$")
#: The most sparklines one request may name (the core's own limit).
MAX_SPARKLINES = 20


def _symbol(value: object) -> str | None:
    return value if isinstance(value, str) and _SYMBOL.match(value) else None


def _number(value: object) -> float | None:
    """A finite JSON number (never a bool), else None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if value == value and abs(value) != float("inf") else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def popup_symbols(status: dict, context: object) -> list[str]:
    """The popup rows' symbols, in order: the context strip, else the bar strip."""
    rows = context.get("cross_market") if isinstance(context, dict) else None
    if not isinstance(rows, list) or not rows:
        rows = status.get("strip")
    symbols: list[str] = []
    for row in rows if isinstance(rows, list) else []:
        symbol = _symbol(row.get("symbol")) if isinstance(row, dict) else None
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols[:MAX_SPARKLINES]


def sparklines_summary(data: object) -> dict | None:
    """The core's 24-hour sparklines, keyed by symbol, values only.

    Each line keeps its first and last bucket times so the popup can say what
    span it covers; a missing bucket stays ``None`` (a gap, never zero).
    """
    if not isinstance(data, dict) or not isinstance(data.get("lines"), list):
        return None
    lines: dict[str, dict] = {}
    for line in data["lines"]:
        if not isinstance(line, dict) or not isinstance(line.get("points"), list):
            continue
        symbol = _symbol(line.get("symbol"))
        points = [p for p in line["points"] if isinstance(p, dict)]
        if not symbol or len(points) != len(line["points"]):
            continue
        lines[symbol] = {
            "start": _text(points[0].get("time")) if points else None,
            "end": _text(points[-1].get("time")) if points else None,
            "values": [_number(p.get("value")) for p in points],
        }
    return {"interval": _text(data.get("interval")),
            "generated_at": _text(data.get("generated_at")), "lines": lines}


def hero_summary(levels: object, symbol: str) -> dict | None:
    """The focus instrument's prior-week range and prior settle, from the core's
    key levels (``GET /api/v1/levels/{symbol}``), with the core's own text."""
    if not isinstance(levels, dict) or levels.get("symbol") != symbol:
        return None
    rows = levels.get("levels")
    by_key = {row.get("key"): row for row in rows if isinstance(row, dict)} \
        if isinstance(rows, list) else {}
    hero: dict[str, object] = {
        "symbol": symbol,
        "trade_date": _text(levels.get("trade_date")),
        "contract": _text(levels.get("contract")),
        "delayed": levels.get("delayed") is True,
    }
    for key in ("prior_week_high", "prior_week_low", "prior_settle"):
        row = by_key.get(key, {})
        hero[key] = _number(row.get("value"))
        hero[f"{key}_text"] = _text(row.get("value_text")) if hero[key] is not None else None
    week = by_key.get("prior_week_high", {})
    hero["week_start"] = _text(week.get("window_start"))
    hero["week_end"] = _text(week.get("window_end"))
    hero["week_unavailable_reason"] = _text(week.get("unavailable_reason"))
    return hero


def collect(base: str) -> dict:
    """Fetch and shape the shell payload. Copies core values verbatim.

    The status endpoint is required; the context endpoint is optional (used by
    the popup for the wider cross-asset strip) and its failure does not fail the
    whole collection. So are the popup's sparklines and the focus instrument's
    prior-week levels (the hero); an instrument without key levels (404) simply
    has no hero.
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

    symbols = popup_symbols(status, context)
    sparklines, sparklines_err = (get(base, "/api/v1/sparklines?symbols=" + ",".join(symbols))
                                  if symbols else (None, None))
    errors["sparklines"] = sparklines_err

    focus = _symbol(status.get("focus")) or "NQ"
    levels, levels_err = get(base, f"/api/v1/levels/{focus}")
    errors["levels"] = None if levels_err == "http 404" else levels_err

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
        "sparklines": sparklines_summary(sparklines),
        "hero": hero_summary(levels, focus),
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
