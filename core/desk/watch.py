"""What to watch: a card before each high-importance release. No AI.

From two hours before a high or critical release until its desk note is
written, Today shows the release's forecasts, the textbook reading of a beat or
a miss for that kind of figure, and what NQ did after this figure's earlier
releases (from the playbook). The readings are fixed rules of thumb, written
once in the explainer library; the market history is counted, never generated.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from core.desk import explainers as X
from core.desk import facts as F
from core.desk.models import Playbook, WatchCard, WatchFigure, WatchReading

#: Cards appear this long before the release.
AHEAD = timedelta(hours=2)

def _watch_reading(family: str) -> WatchReading:
    entry = X.reading(family)
    assert entry.rising is not None and entry.falling is not None
    return WatchReading(family=family, label=entry.title,  # type: ignore[arg-type]
                        above=entry.rising, below=entry.falling)


#: The readings come from the explainer library (``core/desk/explainers.py``).
_READINGS: dict[str, WatchReading] = {
    family: _watch_reading(family) for family, _ in X.READING_CODES}


def reading(code: str) -> WatchReading | None:
    """The rule of thumb for an event code; variants like ``CPI_MOM`` match ``CPI``.

    Families are tried in order, so ``GDP_PRICE_INDEX`` is inflation, not growth.
    """
    family = X.reading_family(code)
    return None if family is None else _READINGS[family]


def cards(events: Iterable[Any], playbook: Playbook | None, *, now: datetime,
          until_written: timedelta, written: Iterable[str] = ()) -> list[WatchCard]:
    """Cards for high-importance releases from :data:`AHEAD` before until their note.

    ``written`` holds the keys of releases whose note exists; their card is gone.
    A card stays at most ``until_written`` after the release.
    """
    done = set(written)
    history = {i.name.casefold(): i for i in (playbook.indicators if playbook else [])}
    result: list[WatchCard] = []
    for slot in F.release_slots(events, start=now - until_written, end=now + AHEAD,
                                min_importance="high"):
        if slot.speakers_only or slot.key in done:
            continue
        readings: list[WatchReading] = []
        for code in slot.codes:
            found = reading(code)
            if found is not None and found not in readings:
                readings.append(found)
        figures: list[WatchFigure] = []
        for figure in slot.figures:
            past = history.get(figure.name.casefold())
            figures.append(WatchFigure(
                name=figure.name,
                forecast_text=figure.forecast_text,
                previous_text=figure.previous_text,
                actual_text=figure.actual_text if figure.actual is not None else None,
                verdict=figure.verdict if figure.actual is not None else None,
                history=[b.text for b in past.buckets] if past else [],
                history_note=(None if past is None else
                              None if past.enough else
                              f"Only {past.count} earlier release"
                              f"{'' if past.count == 1 else 's'}: examples, not a pattern."),
            ))
        result.append(WatchCard(
            key=slot.key, at=slot.at, title=slot.title, importance=slot.importance,
            explainer=F.explainer(slot.primary_code), released=slot.at <= now,
            figures=figures, readings=readings,
        ))
    return result


__all__ = ["AHEAD", "cards", "reading"]
