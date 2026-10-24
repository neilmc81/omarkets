"""Detect the system's IANA time zone without extra dependencies."""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LOCALTIME = Path("/etc/localtime")
TIMEZONE_FILE = Path("/etc/timezone")


def _valid(name: str | None) -> str | None:
    name = (name or "").strip().removeprefix(":")
    if not name or name.startswith("/"):
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None
    return name


def _from_localtime(path: Path) -> str | None:
    try:
        target = str(path.resolve(strict=True))
    except OSError:
        return None
    marker = "zoneinfo/"
    if marker not in target:
        return None
    return _valid(target.rsplit(marker, 1)[1])


def _from_timezone_file(path: Path) -> str | None:
    try:
        return _valid(path.read_text(encoding="utf-8").splitlines()[0])
    except (OSError, IndexError, UnicodeDecodeError):
        return None


def system_timezone() -> str:
    """TZ if it is an IANA name, else /etc/localtime's target, else /etc/timezone, else UTC."""
    return (
        _valid(os.environ.get("TZ"))
        or _from_localtime(LOCALTIME)
        or _from_timezone_file(TIMEZONE_FILE)
        or "UTC"
    )
