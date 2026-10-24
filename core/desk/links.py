"""Which desk note explains a release. Deterministic: matched on the note id.

A release note's id is ``release:<YYYYMMDDTHHMM>:<CODE>[+<CODE>...]``: the
release moment in UTC and every calendar event code out at that moment (see
``ReleaseSlot.key``). A calendar event or playbook release links to the note
with the same UTC minute that names its event code. No text matching.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

RELEASE_PREFIX = "release:"


def release_stamp(at: datetime) -> str:
    """The release-moment part of a note id, always in UTC."""
    return f"{at.astimezone(UTC):%Y%m%dT%H%M}"


def parse_release_id(note_id: str) -> tuple[str, frozenset[str]] | None:
    """``(stamp, codes)`` for a release note id, or None for any other id."""
    if not note_id.startswith(RELEASE_PREFIX):
        return None
    stamp, sep, codes = note_id.removeprefix(RELEASE_PREFIX).partition(":")
    if not sep or len(stamp) != 13 or not codes:
        return None
    try:
        datetime.strptime(stamp, "%Y%m%dT%H%M")
    except ValueError:
        return None
    return stamp, frozenset(code for code in codes.split("+") if code)


class ReleaseNotes:
    """Release notes indexed by their UTC minute, for linking calendar rows to notes."""

    def __init__(self, note_ids: Iterable[str]) -> None:
        self._by_stamp: dict[str, list[tuple[frozenset[str], str]]] = {}
        for note_id in sorted(set(note_ids)):
            parsed = parse_release_id(note_id)
            if parsed is not None:
                stamp, codes = parsed
                self._by_stamp.setdefault(stamp, []).append((codes, note_id))

    def for_event(self, at: datetime | None, code: str) -> str | None:
        """The note for one calendar row released at ``at`` with event ``code``."""
        if at is None:
            return None
        return next((note_id for codes, note_id in self._by_stamp.get(release_stamp(at), [])
                     if code in codes), None)

    def for_slot(self, at: datetime, codes: Iterable[str]) -> str | None:
        """The note for a release moment; the one naming most of its codes wins."""
        wanted = set(codes)
        best: tuple[int, str] | None = None
        for note_codes, note_id in self._by_stamp.get(release_stamp(at), []):
            overlap = len(wanted & note_codes)
            # Ids are visited in sorted order, so ties keep the first id.
            if overlap and (best is None or overlap > best[0]):
                best = (overlap, note_id)
        return best[1] if best else None


__all__ = ["RELEASE_PREFIX", "ReleaseNotes", "parse_release_id", "release_stamp"]
