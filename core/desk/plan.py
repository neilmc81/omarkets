"""The daily plan: one desktop notification before the US session's first data.

No AI. Sent at 07:00 New York time on trading days (evening in Asia), listing
what is scheduled until 17:00 ET in the reader's own time zone, so they know
when to be at the desk.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, time
from zoneinfo import ZoneInfo

from core.desk.models import UpcomingItem

NEW_YORK = ZoneInfo("America/New_York")
#: Sent from this New York time, until :data:`SEND_UNTIL` (e.g. after a restart).
SEND_AT = time(7, 0)
SEND_UNTIL = time(8, 25)
#: The plan covers events up to this New York time (after-close earnings included).
COVERS_UNTIL = time(17, 0)
MAX_LINES = 8


def window(trade_date) -> tuple[datetime, datetime]:
    """When the plan may be sent on ``trade_date``."""
    return (datetime.combine(trade_date, SEND_AT, tzinfo=NEW_YORK),
            datetime.combine(trade_date, SEND_UNTIL, tzinfo=NEW_YORK))


def covers_until(trade_date) -> datetime:
    return datetime.combine(trade_date, COVERS_UNTIL, tzinfo=NEW_YORK)


def plan_text(items: Sequence[UpcomingItem], *, now: datetime, premarket_at: datetime,
              local: ZoneInfo) -> tuple[str, str]:
    """Notification title and body. Times are in the reader's zone (``local``)."""
    today = now.astimezone(local).date()

    def clock(moment: datetime) -> str:
        here = moment.astimezone(local)
        suffix = "" if here.date() == today else f" ({here:%a})"
        return f"{here:%H:%M}{suffix}"

    timed = sorted((i for i in items if i.at is not None and i.at >= now), key=lambda i: i.at)
    lines = []
    for item in timed[:MAX_LINES]:
        flag = " ★" if item.importance in ("critical", "high") else ""
        when = f" · {item.when_text}" if item.when_text else ""
        lines.append(f"{clock(item.at)}  {item.title}{when}{flag}")
    if len(timed) > MAX_LINES:
        lines.append(f"…and {len(timed) - MAX_LINES} more in Coming up")
    lines.append(f"Pre-market note at {clock(premarket_at)}.")
    if not timed:
        return "Omarkets: quiet day ahead", "No scheduled US releases today.\n" + lines[-1]
    big = sum(1 for i in timed if i.importance in ("critical", "high"))
    count = f"{len(timed)} event{'s' if len(timed) != 1 else ''}"
    title = f"Omarkets today: {count}" + (f", {big} important" if big else "")
    return title, "\n".join(lines) + "\n★ = high importance. Times are your time."


__all__ = ["covers_until", "plan_text", "window"]
