"""Logging helpers.

``configure_logging`` is idempotent so it is safe to call from both the CLI
entrypoint and application startup.
"""

from __future__ import annotations

import logging

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%dT%H:%M:%S%z"
_configured = False


def configure_logging(level: str = "INFO") -> None:
    """Configure the root logger once with a compact structured-ish format."""
    global _configured
    resolved = getattr(logging, level.upper(), None)
    if not isinstance(resolved, int):
        resolved = logging.INFO

    root = logging.getLogger()
    if _configured:
        root.setLevel(resolved)
        return

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(resolved)
    _configured = True
