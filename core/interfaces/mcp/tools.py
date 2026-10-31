"""Pure projections from Omarkets API payloads to voice-friendly tool results.

Everything in this module takes already-fetched JSON (a ``dict``) and returns a
compact, typed ``dict`` suitable for a voice client. There is no I/O here, which
is what makes the projections unit-testable without a running backend.

Two invariants:

* **No fabrication.** A missing value becomes ``None`` and is omitted from the
  spoken sentence, never guessed.
* **Stale is spoken.** A quote whose feed is not ``live`` sets a ``stale`` flag
  and the deterministic ``spoken`` text says so, so the assistant cannot give a
  confident live-sounding answer over delayed data.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

#: Spoken, human-friendly labels. Kept short so sentences stay listenable.
SPOKEN_LABELS: dict[str, str] = {
    "NQ": "NQ",
    "ES": "the S&P futures",
    "YM": "the Dow futures",
    "US2Y": "two-year yields",
    "US10Y": "ten-year yields",
    "DXY": "the dollar",
    "VIX": "VIX",
    "VXN": "the Nasdaq volatility index",
}

#: Fallback exchange timezone for events that do not carry their own.
_NEW_YORK = ZoneInfo("America/New_York")

#: Yields are spoken in basis points; everything else in percent.
_BP_SYMBOLS = {"US2Y", "US10Y"}

#: Importance ranking so "most important first" is deterministic.
_IMPORTANCE_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

#: Destinations the Omarkets frontend can actually deep-link to.
#: "overview" and "analysts" open the Today screen (the desk notes).
OPEN_DESTINATIONS = ("overview", "macro", "calendar", "news", "analysts")


class VoiceToolError(RuntimeError):
    """A controlled tool failure with a short, spoken-safe message."""


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):  # NaN/inf
        return None
    return result


def _direction_label(value: float | None) -> str:
    if value is None:
        return ""
    if value > 0:
        return "up"
    if value < 0:
        return "down"
    return "flat"


def spoken_change_pct(value: Any) -> str | None:
    """``-0.72`` -> ``"down 0.72 percent"``; ``0`` -> ``"flat"``.

    A change too small to round above zero is spoken as "flat" rather than a
    misleading "up 0.00 percent".
    """
    number = _num(value)
    if number is None:
        return None
    if round(abs(number), 2) == 0:
        return "flat"
    direction = _direction_label(number)
    return f"{direction} {abs(number):.2f} percent"


def spoken_change_bp(value: Any) -> str | None:
    """``8.4`` -> ``"up 8.4 basis points"``; ``0`` -> ``"flat"``."""
    number = _num(value)
    if number is None:
        return None
    if round(abs(number), 1) == 0:
        return "flat"
    direction = _direction_label(number)
    return f"{direction} {abs(number):.1f} basis points"


def _spoken_value(value: Any, digits: int = 2) -> str | None:
    number = _num(value)
    if number is None:
        return None
    if digits == 0:
        return f"{number:,.0f}"
    return f"{number:,.{digits}f}"


def _quote_line(quote: dict[str, Any]) -> str | None:
    """One spoken clause for a quote, or ``None`` when it cannot be spoken."""
    symbol = str(quote.get("symbol") or "")
    label = SPOKEN_LABELS.get(symbol, symbol or "the market")
    feed = str(quote.get("feed_status") or "")
    if feed in {"unavailable", ""} and _num(quote.get("value")) is None:
        return f"{label} is unavailable"
    if symbol in _BP_SYMBOLS:
        change = spoken_change_bp(quote.get("change_bp"))
        if change is not None:
            return f"{label} are {change}"
        # A yield whose bp change is withheld (e.g. a stale leg) still has a
        # level; report that rather than dropping the whole asset class.
        value = _spoken_value(quote.get("value"), 3)
        if value is None:
            return None
        return f"{label} are at {value} percent"
    if symbol in {"VIX", "VXN"}:
        value = _spoken_value(quote.get("value"), 1)
        if value is None:
            return None
        return f"{label} is at {value}"
    change = spoken_change_pct(quote.get("change_pct"))
    value = _spoken_value(quote.get("value"))
    if change is None and value is None:
        return None
    if change is None:
        return f"{label} is at {value}"
    if value is None:
        return f"{label} is {change}"
    return f"{label} is {change} at {value}"


def _quote_is_live(quote: dict[str, Any]) -> bool:
    return str(quote.get("feed_status") or "") == "live"


def _stale_symbols(quotes: list[dict[str, Any]]) -> list[str]:
    return [str(q.get("symbol")) for q in quotes if not _quote_is_live(q)]


def _find_quote(quotes: list[dict[str, Any]], symbol: str) -> dict[str, Any] | None:
    for quote in quotes:
        if str(quote.get("symbol") or "") == symbol:
            return quote
    return None


def _join_clauses(clauses: list[str]) -> str:
    parts = [c for c in clauses if c]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + ", and " + parts[-1]


def _stale_caveat(symbols: list[str]) -> str:
    if not symbols:
        return ""
    shown = ", ".join(symbols[:3])
    more = " and others" if len(symbols) > 3 else ""
    return f"{shown}{more} data is currently delayed or stale, so don't treat it as a live quote."


# ---------------------------------------------------------------------------
# Market status
# ---------------------------------------------------------------------------


def build_status(
    *,
    status: dict[str, Any] | None,
    context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Compact market picture from ``/shell/status`` plus ``/shell/context``."""
    if not status:
        raise VoiceToolError("Omarkets isn't responding right now.")

    quotes: list[dict[str, Any]] = []
    if context and isinstance(context.get("cross_market"), list):
        quotes = [q for q in context["cross_market"] if isinstance(q, dict)]
    if not quotes and isinstance(status.get("strip"), list):
        quotes = [q for q in status["strip"] if isinstance(q, dict)]

    primary = status.get("primary") if isinstance(status.get("primary"), dict) else None
    data_health = str(status.get("data_health") or "unknown")
    ai_health = str(status.get("ai_health") or "unknown")

    clauses: list[str] = []
    for symbol in ("NQ", "US2Y", "US10Y", "DXY", "VIX"):
        quote = _find_quote(quotes, symbol)
        if quote is None:
            continue
        line = _quote_line(quote)
        if line:
            clauses.append(line)
    spoken = _join_clauses(clauses)

    next_event = status.get("next_event") if isinstance(status.get("next_event"), dict) else None
    if next_event:
        name = str(next_event.get("name") or "").strip()
        # time_et is pre-rendered by the core, which owns timezones.
        when = str(next_event.get("time_et") or "").strip()
        if name:
            sentence = f"Next on the calendar is {name}{f' at {when}' if when else ''}."
            spoken = f"{spoken.rstrip('.')}. {sentence}".strip() if spoken else sentence

    caveat = _stale_caveat(_stale_symbols(quotes))
    if caveat:
        spoken = (caveat + " " + spoken).strip() if spoken else caveat

    return {
        "spoken": spoken or "I couldn't assemble a market picture right now.",
        "as_of": status.get("generated_at"),
        "primary": primary,
        "quotes": quotes,
        "session": status.get("session"),
        "next_event": next_event,
        "data_health": data_health,
        "ai_health": ai_health,
        "stale": bool(caveat),
    }


# ---------------------------------------------------------------------------
# Brief
# ---------------------------------------------------------------------------


def build_brief(
    *,
    status: dict[str, Any] | None,
    context: dict[str, Any] | None,
    note: dict[str, Any] | None,
) -> dict[str, Any]:
    """A brief built only from existing state and the latest stored desk note."""
    status_view = build_status(status=status, context=context)
    sections: list[str] = []
    if status_view["spoken"]:
        sections.append(status_view["spoken"])
    note_view = _note_view(note)
    if note_view:
        sections.append(note_view["spoken"])
    return {
        "spoken": " ".join(s for s in sections if s) or "No market brief is available right now.",
        "as_of": status.get("generated_at") if isinstance(status, dict) else None,
        "market": status_view,
        "analysis": note_view,
    }


# ---------------------------------------------------------------------------
# Next event / calendar
# ---------------------------------------------------------------------------


def build_next_event(
    events: list[dict[str, Any]],
    *,
    name: str | None = None,
) -> dict[str, Any]:
    """The next calendar event, optionally filtered by a name/code substring."""
    candidates = [e for e in events if isinstance(e, dict)]
    if name:
        needle = name.strip().lower()
        candidates = [
            e
            for e in candidates
            if needle in str(e.get("name") or "").lower()
            or needle == str(e.get("event_code") or "").lower()
        ]
    if not candidates:
        label = f" for {name}" if name else ""
        return {"spoken": f"I don't see an upcoming event{label}.", "event": None, "events": []}
    event = _next_by_time(candidates)
    return {
        # The event sentence already names the event, so the lead must not
        # repeat it ("Next match: CPI at 08:30" not "Your next CPI: CPI ...").
        "spoken": _event_sentence(event, lead="Next match" if name else "Next up"),
        "event": event,
        "events": candidates[:5],
    }


def build_calendar(
    events: list[dict[str, Any]],
    *,
    scope: str = "today",
    include_all: bool = False,
) -> dict[str, Any]:
    """Calendar events, prioritising CRITICAL/HIGH unless ``include_all``."""
    rows = [e for e in events if isinstance(e, dict)]
    if not include_all:
        rows = [e for e in rows if str(e.get("importance") or "") in {"critical", "high"}]
    rows = sorted(
        rows,
        key=lambda e: (
            _IMPORTANCE_ORDER.get(str(e.get("importance") or ""), 9),
            str(e.get("scheduled_at") or ""),
        ),
    )
    if not rows:
        where = {"today": "today", "week": "this week", "upcoming": "upcoming"}.get(scope, scope)
        return {"spoken": f"There are no key events {where}.", "scope": scope, "events": []}
    names: list[str] = []
    for event in rows[:4]:
        time_et = _event_time(event)
        label = str(event.get("name") or event.get("event_code") or "event")
        names.append(f"{label}{' at ' + time_et if time_et else ''}")
    where = {"today": "today", "week": "this week", "upcoming": "coming up"}.get(scope, scope)
    spoken = f"Key events {where}: " + "; ".join(names) + "."
    return {"spoken": spoken, "scope": scope, "events": rows}


def _event_time(event: dict[str, Any]) -> str:
    """Display time for an event.

    ``/shell/status`` pre-renders ``time_et``; the calendar endpoints return a
    bare ``EconomicEvent`` with only ``scheduled_at``/``released_at`` and a
    ``timezone``. The core owns timezone conversion, but the voice adapter must
    still present a time rather than silently omit it, so this is the one place
    we format an instant — using the event's own exchange timezone.
    """
    pre = str(event.get("time_et") or "").strip()
    if pre:
        return pre
    moment = _parse_iso(event.get("scheduled_at") or event.get("released_at"))
    if moment is None:
        return ""
    tz_name = str(event.get("timezone") or "").strip()
    zone: Any = _NEW_YORK
    if tz_name:
        try:
            zone = ZoneInfo(tz_name)
        except (KeyError, ValueError):
            zone = _NEW_YORK
    local = moment.astimezone(zone)
    return local.strftime("%H:%M %Z")


def _next_by_time(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    ordered = [e for e in events if e.get("scheduled_at")]
    if not ordered:
        return events[0] if events else None
    return min(ordered, key=lambda e: str(e.get("scheduled_at")))


def _event_sentence(event: dict[str, Any] | None, *, lead: str) -> str:
    if not event:
        return ""
    name = str(event.get("name") or event.get("event_code") or "the event")
    time_et = _event_time(event)
    parts = [f"{lead}: {name}"]
    if time_et:
        parts.append(f"at {time_et}")
    consensus = _num(event.get("consensus"))
    if consensus is not None:
        parts.append(f"consensus {_spoken_value(consensus)}")
    previous = _num(event.get("previous"))
    if previous is not None:
        parts.append(f"previous {_spoken_value(previous)}")
    return " ".join(parts).rstrip(".") + "."


# ---------------------------------------------------------------------------
# Macro
# ---------------------------------------------------------------------------


def build_macro(
    state: dict[str, Any] | None,
    *,
    series: str | None = None,
    limit: int = 6,
) -> dict[str, Any]:
    """Latest macro state, optionally narrowed to one series id substring."""
    if not state or not isinstance(state.get("series"), list):
        return {"spoken": "Macro data is unavailable right now.", "series": []}

    rows: list[dict[str, Any]] = []
    for entry in state["series"]:
        if not isinstance(entry, dict):
            continue
        definition = entry.get("definition") if isinstance(entry.get("definition"), dict) else {}
        series_id = str(definition.get("id") or "")
        if series and series.strip().lower() not in series_id.lower():
            continue
        latest = entry.get("latest") if isinstance(entry.get("latest"), dict) else None
        rows.append(
            {
                "id": series_id,
                "name": definition.get("name"),
                "unit": definition.get("unit"),
                "status": entry.get("status"),
                "latest": latest,
            }
        )
    if series and not rows:
        return {"spoken": f"I don't have a macro series for {series}.", "series": []}

    clauses: list[str] = []
    for row in rows[:limit]:
        latest = row.get("latest") or {}
        value = latest.get("value")
        if value is None:
            continue
        name = str(row.get("name") or row.get("id") or "the series")
        unit = str(row.get("unit") or "").strip()
        period = str(latest.get("period") or "").strip()
        spoken_value = _spoken_value(value, 2 if abs(_num(value) or 0) < 100 else 1)
        clause = f"{name} is {spoken_value}"
        if unit:
            clause += f" {unit}"
        if period:
            clause += f" for {period}"
        clauses.append(clause)
    spoken = (
        "Latest macro readings: " + "; ".join(clauses) + "."
        if clauses
        else "No macro readings are available."
    )
    return {
        "spoken": spoken,
        "as_of": state.get("generated_at"),
        "mode": state.get("mode"),
        "series": rows,
    }


# ---------------------------------------------------------------------------
# Technical
# ---------------------------------------------------------------------------


def build_latest_events(
    events: list[dict[str, Any]],
    *,
    materiality: dict[str, str] | None = None,
    now: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Omarkets-ranked material events, never the raw firehose.

    Selection is presentation-only: it does not re-rank Omarkets materiality.
    It drops future-dated scheduled releases (they are calendar items, not news
    that happened) when they would crowd out real events, and collapses repeated
    headline families (e.g. four monthly instances of the same release) so a
    spoken answer stays listenable.
    """
    rows = [e for e in events if isinstance(e, dict)]
    if not rows:
        return {"spoken": "I don't see any important market events right now.", "events": []}
    levels = materiality or {}

    def importance(event: dict[str, Any]) -> int:
        event_id = str(event.get("id") or "")
        return _IMPORTANCE_ORDER.get(levels.get(event_id, ""), 4)

    # Most important first, and within one importance level the most recent
    # first: this is "latest events", and the API already returns newest-first.
    # Sorting ascending by ``published_at`` would surface the oldest instance of
    # a repeated release and read out stale news as if it were current.
    ranked = sorted(rows, key=lambda e: str(e.get("published_at") or ""), reverse=True)
    ranked.sort(key=importance)
    now_dt = _parse_iso(now) if now else None
    if now_dt is not None:
        past = [e for e in ranked if _is_at_or_before(e.get("published_at"), now_dt)]
        # Only prefer past events if there are enough of them; otherwise the
        # future item is the only material thing we have.
        if len(past) >= 1:
            ranked = past

    selected: list[dict[str, Any]] = []
    seen_families: list[set[str]] = []
    for event in ranked:
        tokens = _tokens(str(event.get("headline") or ""))
        if tokens and any(
            len(tokens & family) >= max(2, len(tokens) - 1) for family in seen_families
        ):
            continue
        seen_families.append(tokens)
        selected.append(event)
        if len(selected) >= limit:
            break

    out = [
        {
            "event_id": str(event.get("id") or ""),
            "headline": event.get("headline"),
            "event_type": event.get("event_type"),
            "published_at": event.get("published_at"),
            "instruments": event.get("instruments") or [],
            "countries": event.get("countries") or [],
            "materiality": levels.get(str(event.get("id") or "")),
            # A scheduled item that has not happened yet must not be read aloud
            # as if it had. The presenter can then say "upcoming" instead.
            "upcoming": bool(
                now_dt is not None and not _is_at_or_before(event.get("published_at"), now_dt)
            ),
        }
        for event in selected
    ]
    headlines = [str(e.get("headline")) for e in out if e.get("headline")]
    if not headlines:
        return {"spoken": "I don't see any important market events right now.", "events": []}
    spoken_parts = []
    for item in out:
        if not item.get("headline"):
            continue
        prefix = "coming up: " if item.get("upcoming") else ""
        spoken_parts.append(f"{prefix}{item['headline']}.")
        if len(spoken_parts) >= 3:
            break
    spoken = "Important right now: " + " ".join(spoken_parts)
    return {"spoken": spoken, "events": out}


def match_recent_event(
    events: list[dict[str, Any]],
    descriptor: str,
) -> dict[str, Any] | None:
    """Resolve a spoken descriptor to a real recent event, or ``None``.

    Conservative by construction so the assistant cannot invent an event or
    explain the wrong one:

    * two or more meaningful shared tokens always win (and beat a single shared
      token), or
    * a single shared token is accepted only when exactly one event matches it.

    Returns the matched event's id and headline so the caller can echo which
    event it resolved.
    """
    needle = _tokens(descriptor)
    if not needle:
        return None
    scored: list[tuple[int, dict[str, Any]]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        haystack = _tokens(
            str(event.get("headline") or "") + " " + str(event.get("event_type") or "")
        )
        score = len(needle & haystack)
        if score >= 1:
            scored.append((score, event))
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    best_score = scored[0][0]
    if best_score < 2:
        # A single shared token: only safe if it is unambiguous.
        if sum(1 for score, _ in scored if score == 1) != 1:
            return None
    best = scored[0][1]
    return {
        "event_id": str(best.get("id") or "") or None,
        "headline": best.get("headline"),
        "score": best_score,
    }


_STOPWORDS = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "of",
    "to",
    "in",
    "on",
    "for",
    "with",
    "about",
    "what",
    "that",
    "this",
    "it",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "explain",
    "latest",
    "market",
    "markets",
    "news",
    "story",
    "event",
    "say",
    "says",
    "tell",
    "me",
    "please",
    "happened",
    "happening",
    "after",
    "before",
}


def _tokens(text: str) -> set[str]:
    cleaned = "".join(ch if ch.isalnum() else " " for ch in text.lower())
    return {word for word in cleaned.split() if len(word) >= 3 and word not in _STOPWORDS}


def _parse_iso(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp, tolerating both ``Z`` and ``+00:00``.

    The API emits ``...Z`` while the server's ``now`` uses ``+00:00``; comparing
    those as raw strings misclassifies a same-second event as future.
    """
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _is_at_or_before(value: Any, reference: datetime) -> bool:
    """True unless ``value`` is a parseable timestamp strictly after ``reference``.

    An unparseable or missing timestamp is treated as "not future" so an event
    without a publication time is never silently hidden.
    """
    parsed = _parse_iso(value)
    if parsed is None:
        return True
    return parsed <= reference


def build_open_omarkets(destination: str | None) -> dict[str, Any]:
    """Validate an open/deep-link destination. Launching is done by the server."""
    dest = (destination or "overview").strip().lower()
    if dest not in OPEN_DESTINATIONS:
        raise VoiceToolError(
            "I can open Omarkets, but only the overview, macro, calendar, news or analysts pages."
        )
    return {"destination": dest, "spoken": f"Opening Omarkets {dest}."}


# ---------------------------------------------------------------------------
# Desk notes (the plain-English notes on the Today screen)
# ---------------------------------------------------------------------------

#: Spoken labels for desk note kinds.
NOTE_LABELS: dict[str, str] = {
    "primer": "week-ahead note",
    "premarket": "pre-market note",
    "release": "economic-release note",
    "fed": "Fed note",
    "earnings": "earnings note",
    "move": "big-move note",
    "ask": "market-now note",
    "review": "weekly review",
    "auction": "Treasury auction note",
}

#: Note kinds a "briefing" request may return.
BRIEFING_KINDS = ("premarket", "primer", "review")


def _note_view(note: dict[str, Any] | None) -> dict[str, Any] | None:
    """One stored desk note, or ``None`` when it has no written text."""
    if not isinstance(note, dict) or note.get("status") != "ok":
        return None
    draft = note.get("draft")
    if not isinstance(draft, dict) or not draft.get("headline"):
        return None
    label = NOTE_LABELS.get(str(note.get("kind") or ""), "note")
    headline = str(draft.get("headline")).strip().rstrip(".")
    summary = str(draft.get("summary") or "").strip()
    return {
        "note_id": note.get("id"),
        "kind": note.get("kind"),
        "title": note.get("title"),
        "created_at": note.get("created_at"),
        "headline": draft.get("headline"),
        "summary": summary,
        "nq_tone": draft.get("nq_tone") or draft.get("tone"),
        "model": note.get("model"),
        "spoken": f"The latest {label}: {headline}. {summary}".strip(),
    }


def _ok_notes(notes: list[dict[str, Any]], kinds: tuple[str, ...] | None = None):
    for note in notes:
        if kinds and str((note or {}).get("kind") or "") not in kinds:
            continue
        view = _note_view(note)
        if view is not None:
            yield view


def build_latest_analysis(
    notes: list[dict[str, Any]],
    *,
    kind: str | None = None,
) -> dict[str, Any]:
    """The newest written desk note, optionally of one kind."""
    view = next(_ok_notes(notes, (kind,) if kind else None), None)
    if view is None:
        return {"spoken": "There's no Omarkets note available right now.", "analysis": None}
    return {"spoken": view["spoken"], "analysis": view}


def build_latest_briefing(
    notes: list[dict[str, Any]],
    *,
    briefing_type: str | None = None,
) -> dict[str, Any]:
    """The newest pre-market, week-ahead or weekly-review note."""
    kinds = (briefing_type,) if briefing_type in BRIEFING_KINDS else BRIEFING_KINDS
    view = next(_ok_notes(notes, kinds), None)
    if view is None:
        return {"spoken": "I don't have a prepared briefing right now.", "briefing": None}
    return {"spoken": view["spoken"], "briefing": view}


def build_event_note(
    notes: list[dict[str, Any]],
    descriptor: str,
) -> dict[str, Any]:
    """The desk note about the event the user described, never a guess."""
    candidates = [
        {
            "id": view["note_id"],
            "headline": f"{view['title']} {view['headline']}",
            "event_type": view["kind"],
        }
        for view in _ok_notes(notes)
    ]
    match = match_recent_event(candidates, descriptor)
    if not match:
        return {
            "spoken": "I couldn't match that to a recent Omarkets note.",
            "event_id": None,
            "analyses": [],
        }
    view = next(v for v in _ok_notes(notes) if v["note_id"] == match["event_id"])
    return {"spoken": view["spoken"], "event_id": view["note_id"], "analyses": [view]}


def build_note(note: dict[str, Any] | None, *, note_id: str) -> dict[str, Any]:
    view = _note_view(note)
    if view is None:
        return {
            "spoken": "I don't have a Omarkets note for that.",
            "event_id": note_id,
            "analyses": [],
        }
    return {"spoken": view["spoken"], "event_id": note_id, "analyses": [view]}


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------


def build_usage(budget: dict[str, Any] | None) -> dict[str, Any]:
    """This week's Omarkets AI spend from the cost guard, separate from voice billing."""
    if not isinstance(budget, dict):
        raise VoiceToolError("Omarkets AI spending is unavailable right now.")
    spent = _num(budget.get("week_spent_usd"))
    limit = _num(budget.get("weekly_limit_usd"))
    remaining = _num(budget.get("week_remaining_usd"))
    if spent is None:
        spoken = "Omarkets AI spending is unavailable right now."
    else:
        spoken = f"This week Omarkets has spent about ${spent:.2f} on AI notes"
        if limit:
            spoken += f" of its ${limit:.2f} weekly limit"
        spoken += ". That excludes voice usage."
    if budget.get("paused"):
        spoken += " Paid AI is paused: " + str(budget.get("reason") or "limit reached") + "."
    return {
        "spoken": spoken,
        "week_spent_usd": spent,
        "weekly_limit_usd": limit,
        "week_remaining_usd": remaining,
        "paused": bool(budget.get("paused")),
    }


#: Voice tools whose backing features were removed on 2026-10-20 with the old
#: Jev/analyst chain. The names stay so the voice project's tool list still
#: matches; each answers plainly that the feature is gone.
RETIRED_TOOLS = (
    "market_history_query",
    "market_materiality_calibration",
    "market_fomc_history",
    "market_similar_cases",
    "market_calibration_review",
)


def build_retired() -> dict[str, Any]:
    return {
        "spoken": "That Omarkets feature was retired in October 2026. "
        "Ask for the latest Omarkets note instead.",
        "available": False,
    }
