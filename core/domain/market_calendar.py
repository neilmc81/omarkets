"""Deterministic US equity-market calendar.

This module answers one question: **what kind of day is this?** It is the single
source of truth for weekends, NYSE holidays and early-close (half) days, so the
session engine, bar bucketing, and the historical layer all agree.

Rules are computed from the calendar — never a hand-maintained yearly list:

* **New Year's Day** — January 1, moved to the nearest weekday when it falls on
  a weekend (a Saturday January 1 closes the prior Friday, December 31).
* **Martin Luther King Jr. Day** — third Monday of January.
* **Washington's Birthday / Presidents' Day** — third Monday of February.
* **Good Friday** — the Friday before Easter (Gregorian computus).
* **Memorial Day** — last Monday of May.
* **Juneteenth** — June 19, observed from 2022.
* **Independence Day** — July 4, moved to the nearest weekday.
* **Labor Day** — first Monday of September.
* **Thanksgiving** — fourth Thursday of November.
* **Christmas Day** — December 25, moved to the nearest weekday.

**Early-close days** (NYSE closes 13:00 ET):

* the Friday after Thanksgiving;
* Christmas Eve (December 24) when it is a trading day;
* July 3 when both July 3 and July 4 are trading weekdays.

Ad-hoc exchange closures (storm days, national days of mourning) are announced
by the exchange and are deliberately **not** guessed here. A date this module
does not know about is treated as a normal trading day, and derived values fall
back to ``insufficient_data`` rather than invented levels.
"""

from __future__ import annotations

from datetime import date, time, timedelta
from enum import StrEnum


class TradingDayKind(StrEnum):
    """What kind of market day a calendar date is."""

    REGULAR = "regular"
    EARLY_CLOSE = "early_close"
    HOLIDAY = "holiday"
    WEEKEND = "weekend"


#: Cash-equity close on an early-close day (NYSE is 13:00 ET).
EARLY_CLOSE_TIME = time(13, 0)
#: CME equity-index futures Globex halt on an early-close day (13:15 ET).
EARLY_CLOSE_GLOBEX_TIME = time(13, 15)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The ``n``-th ``weekday`` (0=Mon) of a month, 1-indexed."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """The last ``weekday`` of a month."""
    if month == 12:
        last = date(year, 12, 31)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    offset = (last.weekday() - weekday) % 7
    return last - timedelta(days=offset)


def observed(day: date) -> date:
    """Move a fixed-date holiday to the nearest weekday (US federal rule)."""
    if day.weekday() == 5:  # Saturday -> Friday
        return day - timedelta(days=1)
    if day.weekday() == 6:  # Sunday -> Monday
        return day + timedelta(days=1)
    return day


def easter(year: int) -> date:
    """Easter Sunday (anonymous Gregorian computus)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def good_friday(year: int) -> date:
    return easter(year) - timedelta(days=2)


def thanksgiving(year: int) -> date:
    """Fourth Thursday of November."""
    return _nth_weekday(year, 11, 3, 4)


#: NYSE first observed Juneteenth in 2022.
_JUNETEENTH_FIRST_YEAR = 2022


def _holiday_map(year: int) -> dict[date, str]:
    """All observed NYSE holidays that fall in ``year``, by date."""
    holidays: dict[date, str] = {}
    new_year = observed(date(year, 1, 1))
    if new_year.year == year:
        holidays[new_year] = "New Year's Day"
    # A Saturday January 1 of the NEXT year is observed on December 31 of this
    # year, so it belongs to this year's map.
    next_new_year = observed(date(year + 1, 1, 1))
    if next_new_year.year == year:
        holidays[next_new_year] = "New Year's Day"
    holidays[_nth_weekday(year, 1, 0, 3)] = "Martin Luther King Jr. Day"
    holidays[_nth_weekday(year, 2, 0, 3)] = "Washington's Birthday"
    holidays[good_friday(year)] = "Good Friday"
    holidays[_last_weekday(year, 5, 0)] = "Memorial Day"
    if year >= _JUNETEENTH_FIRST_YEAR:
        holidays[observed(date(year, 6, 19))] = "Juneteenth"
    holidays[observed(date(year, 7, 4))] = "Independence Day"
    holidays[_nth_weekday(year, 9, 0, 1)] = "Labor Day"
    holidays[thanksgiving(year)] = "Thanksgiving Day"
    holidays[observed(date(year, 12, 25))] = "Christmas Day"
    return holidays


_HOLIDAY_CACHE: dict[int, dict[date, str]] = {}


def holidays(year: int) -> dict[date, str]:
    """Observed NYSE holidays whose observed date falls in ``year``."""
    cached = _HOLIDAY_CACHE.get(year)
    if cached is None:
        cached = _holiday_map(year)
        if len(_HOLIDAY_CACHE) > 32:  # bound the cache for a long-lived process
            for stale in sorted(_HOLIDAY_CACHE)[:-16]:
                _HOLIDAY_CACHE.pop(stale, None)
        _HOLIDAY_CACHE[year] = cached
    return cached


def holiday_name(day: date) -> str | None:
    """The observed holiday name for a date, or ``None``."""
    return holidays(day.year).get(day)


def is_weekend(day: date) -> bool:
    return day.weekday() >= 5


def is_holiday(day: date) -> bool:
    return holiday_name(day) is not None


def is_early_close(day: date) -> bool:
    """Whether the exchange closes early (13:00 ET) on this date."""
    if is_weekend(day) or is_holiday(day):
        return False
    # Friday after Thanksgiving.
    if day == thanksgiving(day.year) + timedelta(days=1):
        return True
    # Christmas Eve when it is a trading day.
    if day.month == 12 and day.day == 24:
        return True
    # July 3 when Independence Day falls on a weekday (so July 3 is a normal
    # trading day before the July 4 holiday). When July 4 is a weekend, July 3
    # is the observed holiday and there is no separate early close.
    if day.month == 7 and day.day == 3:
        july4 = date(day.year, 7, 4)
        if july4.weekday() < 5:
            return True
    return False


def trading_day_kind(day: date) -> TradingDayKind:
    """Classify a calendar date."""
    if is_weekend(day):
        return TradingDayKind.WEEKEND
    if is_holiday(day):
        return TradingDayKind.HOLIDAY
    if is_early_close(day):
        return TradingDayKind.EARLY_CLOSE
    return TradingDayKind.REGULAR


def is_trading_day(day: date) -> bool:
    """True for a regular or early-close NYSE trading day."""
    return trading_day_kind(day) in {TradingDayKind.REGULAR, TradingDayKind.EARLY_CLOSE}


def rth_close_time(day: date) -> time:
    """The cash-equity close time for a date (13:00 ET on early-close days)."""
    return EARLY_CLOSE_TIME if is_early_close(day) else time(16, 0)


def next_trading_day(day: date) -> date:
    candidate = day + timedelta(days=1)
    while not is_trading_day(candidate):
        candidate += timedelta(days=1)
    return candidate


def previous_trading_day(day: date) -> date:
    candidate = day - timedelta(days=1)
    while not is_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def trading_days(start: date, end: date) -> list[date]:
    """Trading days in the half-open range ``[start, end)``."""
    days: list[date] = []
    cursor = start
    while cursor < end:
        if is_trading_day(cursor):
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


__all__ = [
    "EARLY_CLOSE_GLOBEX_TIME",
    "EARLY_CLOSE_TIME",
    "TradingDayKind",
    "easter",
    "good_friday",
    "holiday_name",
    "holidays",
    "is_early_close",
    "is_holiday",
    "is_trading_day",
    "is_weekend",
    "next_trading_day",
    "observed",
    "previous_trading_day",
    "rth_close_time",
    "thanksgiving",
    "trading_day_kind",
    "trading_days",
]
