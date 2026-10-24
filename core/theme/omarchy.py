"""Read the active Omarchy theme palette so the UI can match the desktop.

The authoritative palette is ``~/.local/state/omarchy/current/theme/colors.toml``.
``omarchy theme set`` builds the next theme in a staging directory and swaps it in
with ``rm -rf`` + ``mv``, then runs ``~/.config/omarchy/hooks/theme-set.d/``. So
the file is re-read whenever its content changes (checked by hash, not by
inotify, which would not survive the directory swap), and the hook calls
``POST /api/v1/theme/reload`` to make a switch visible at once.

When Omarchy is not installed, or the file is missing or unreadable, a built-in
palette is served with ``source="fallback"``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

logger = logging.getLogger(__name__)

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

#: Every palette role the UI maps into design tokens.
ROLES = (
    "background", "foreground", "bright_foreground", "accent", "selection", "muted",
    "red", "green", "yellow", "orange", "blue", "cyan", "magenta",
)

#: Older themes ship only terminal colours; Omarchy maps them the same way.
LEGACY_KEYS = {
    "background": "color0", "red": "color1", "green": "color2", "yellow": "color3",
    "blue": "color4", "magenta": "color5", "cyan": "color6", "foreground": "color7",
    "muted": "color8",
}

#: Shown when Omarchy is unavailable (Omarkets's original dark palette).
FALLBACK = {
    "background": "#090f18", "foreground": "#dbe5f1", "bright_foreground": "#ffffff",
    "accent": "#71d6ff", "selection": "#1d3a55", "muted": "#8396ab",
    "red": "#ff6b6b", "green": "#4fd18b", "yellow": "#f2c14e", "orange": "#e0a050",
    "blue": "#7aa2f7", "cyan": "#62c9ed", "magenta": "#cdb8f0",
}


class ThemePalette(BaseModel):
    """The palette the UI turns into its design tokens."""

    source: Literal["omarchy", "fallback"]
    name: str | None
    mode: Literal["dark", "light"]
    #: Changes whenever any colour changes; clients compare it to skip work.
    version: str
    colors: dict[str, str]
    detail: str | None = None


def default_theme_dir() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "omarchy" / "current" / "theme"


def _luminance(hex_color: str) -> float:
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _version(colors: dict[str, str], mode: str, name: str | None) -> str:
    digest = hashlib.sha256()
    for key in sorted(colors):
        digest.update(f"{key}={colors[key]};".encode())
    digest.update(f"mode={mode};name={name}".encode())
    return digest.hexdigest()[:16]


def fallback_palette(detail: str) -> ThemePalette:
    colors = dict(FALLBACK)
    return ThemePalette(source="fallback", name=None, mode="dark",
                        version=_version(colors, "dark", None), colors=colors, detail=detail)


def parse_palette(raw: str, name: str | None) -> ThemePalette:
    """Map a ``colors.toml`` document onto the UI roles.

    Invalid or missing colours fall back role by role, so a theme that omits,
    say, ``orange`` still renders; a document without usable background and
    foreground is rejected.
    """
    data = tomllib.loads(raw)

    def pick(key: str) -> str | None:
        value = data.get(key)
        if isinstance(value, str) and HEX.match(value):
            return value.lower()
        legacy = LEGACY_KEYS.get(key)
        value = data.get(legacy) if legacy else None
        if isinstance(value, str) and HEX.match(value):
            return value.lower()
        return None

    background, foreground = pick("background"), pick("foreground")
    if background is None or foreground is None:
        raise ValueError("colors.toml has no valid background/foreground")
    colors: dict[str, str] = {"background": background, "foreground": foreground}
    derived = {
        "bright_foreground": foreground,
        "accent": pick("blue") or foreground,
        "selection": pick("lighter_background") or pick("muted") or foreground,
        "muted": foreground,
        "orange": pick("yellow") or FALLBACK["orange"],
    }
    for role in ROLES:
        if role in colors:
            continue
        colors[role] = pick(role) or derived.get(role) or FALLBACK[role]
    mode_value = data.get("mode")
    if mode_value in ("dark", "light"):
        mode = mode_value
    else:
        mode = "light" if _luminance(background) > 0.5 else "dark"
    return ThemePalette(source="omarchy", name=name, mode=mode,
                        version=_version(colors, mode, name), colors=colors)


class OmarchyTheme:
    """Cached reader of the active theme with change notification."""

    def __init__(self, theme_dir: Path | None = None) -> None:
        self.theme_dir = theme_dir or default_theme_dir()
        self._digest: str | None = None
        self._palette: ThemePalette | None = None
        # Replaced on every wake-up so each waiter sees exactly one signal.
        self._wake = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        #: Set at process exit so open streams end instead of holding shutdown.
        self.closed = False

    @property
    def colors_path(self) -> Path:
        return self.theme_dir / "colors.toml"

    def _theme_name(self) -> str | None:
        try:
            name = (self.theme_dir.parent / "theme.name").read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return name[:80] or None

    def current(self) -> ThemePalette:
        """The active palette, re-parsed only when ``colors.toml`` changed."""
        try:
            raw = self.colors_path.read_bytes()
        except OSError:
            raw = None
        name = self._theme_name()
        digest = hashlib.sha256((raw or b"") + (name or "").encode()).hexdigest()
        if self._palette is not None and digest == self._digest:
            return self._palette
        if raw is None:
            palette = fallback_palette("Omarchy theme not found; using the built-in palette")
        else:
            try:
                palette = parse_palette(raw.decode("utf-8"), name)
            except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError) as exc:
                logger.warning("omarchy theme unreadable (%s); using fallback", type(exc).__name__)
                palette = fallback_palette(
                    "Omarchy theme could not be read; using the built-in palette")
        previous = self._palette
        self._digest, self._palette = digest, palette
        if previous is not None and previous.version != palette.version:
            logger.info("omarchy theme changed to %s (%s)", palette.name, palette.source)
        return palette

    def _signal(self) -> None:
        wake, self._wake = self._wake, asyncio.Event()
        wake.set()

    async def notify(self) -> ThemePalette:
        """Re-read now and wake every waiting stream (the theme-set hook path)."""
        palette = self.current()
        self._signal()
        return palette

    def close(self) -> None:
        """End open streams. Safe to call from a signal handler."""
        self.closed = True
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._signal)

    async def wait_for_change(self, version: str, within: float) -> ThemePalette:
        """Return as soon as the palette differs from ``version``, after ``within``
        seconds, or when closed.

        Polls the file every second as a fallback for a missing hook.
        """
        loop = self._loop = asyncio.get_running_loop()
        deadline = loop.time() + within
        while True:
            palette = self.current()
            remaining = deadline - loop.time()
            if self.closed or palette.version != version or remaining <= 0:
                return palette
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=min(1.0, remaining))
            except TimeoutError:
                pass
