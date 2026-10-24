"""Notification delivery boundary.

The only module that knows how to invoke a desktop notification. It shells out
to ``notify-send`` (present on Omarchy), which talks to the running shell's
notification daemon. Delivery is best-effort and never blocks or fails the core:
a missing binary or a non-graphical session is reported, not raised.

Tests inject a fake notifier; no automated test invokes a real desktop.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)

#: notify-send icon for alerts. A stock freedesktop name is used so no asset has
#: to be installed and the shell theme resolves it.
ALERT_ICON = "dialog-warning"


class Notifier(ABC):
    """Delivers one desktop notification. Must not raise for an environment fault."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable identifier, e.g. ``notify_send``."""

    @abstractmethod
    async def deliver_text(
        self, summary: str, body: str, *, urgency: str = "normal", icon: str | None = None
    ) -> tuple[bool, str | None]:
        """Deliver one notification. Returns ``(delivered, error)``."""

    async def aclose(self) -> None:
        return None


class NotifySendNotifier(Notifier):
    """Delivers via the ``notify-send`` CLI.

    The command is built from a fixed argument vector; the headline and body are
    passed as separate argv entries so no shell parsing happens and notification
    text can never be executed as a command.
    """

    def __init__(self, *, command: str = "notify-send", app_name: str = "Omarkets") -> None:
        self.command = command
        self.app_name = app_name

    @property
    def name(self) -> str:
        return "notify_send"

    async def deliver_text(
        self, summary: str, body: str, *, urgency: str = "normal", icon: str | None = None
    ) -> tuple[bool, str | None]:
        if urgency not in {"low", "normal", "critical"}:
            raise ValueError(f"invalid urgency {urgency!r}")
        argv = [
            self.command,
            "--urgency",
            urgency,
            "--app-name",
            self.app_name,
            "--category",
            "market",
            "--icon",
            icon or "dialog-information",
            summary,
            body,
        ]
        return await self._send(argv)

    async def _send(self, argv: list[str]) -> tuple[bool, str | None]:
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return False, f"{self.command} not found"
        except Exception as exc:  # pragma: no cover - defensive
            return False, f"{type(exc).__name__}: {exc}"
        try:
            _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5.0)
        except TimeoutError:
            process.kill()
            return False, "notify-send timed out"
        if process.returncode != 0:
            detail = (stderr or b"").decode("utf-8", "replace").strip()[:200]
            return False, detail or f"exit {process.returncode}"
        return True, None


class FakeNotifier(Notifier):
    """Records notifications in memory. Used by tests."""

    def __init__(self) -> None:
        self.delivered: list[tuple[str, str, str]] = []

    @property
    def name(self) -> str:
        return "fake"

    async def deliver_text(
        self, summary: str, body: str, *, urgency: str = "normal", icon: str | None = None
    ) -> tuple[bool, str | None]:
        self.delivered.append((summary, body, urgency))
        return True, None


__all__ = ["ALERT_ICON", "FakeNotifier", "Notifier", "NotifySendNotifier"]
