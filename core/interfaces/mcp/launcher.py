"""Launch/focus the Omarkets workstation for the ``open_omarkets`` voice tool.

Reuses the existing Phase 8 shell script (``omarchy/omarkets-launch``) so the
deep-link and focus-an-existing-window behaviour is identical to the bar widget
and hotkeys. Nothing here starts the backend; the user service owns that.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

#: Where the installer symlinks the launcher; also checked in-repo for dev.
_LAUNCHER_CANDIDATES = (
    "~/.local/bin/omarkets-launch",
    "~/.config/omarchy/plugins/omarkets/../../omarkets-launch",
)

VALID_DESTINATIONS = ("overview", "macro", "calendar", "news", "analysts")


def _resolve_launcher() -> str | None:
    found = shutil.which("omarkets-launch")
    if found:
        return found
    for candidate in _LAUNCHER_CANDIDATES:
        path = Path(os.path.expanduser(candidate))
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    # Last resort: the repo copy relative to this file.
    repo = Path(__file__).resolve().parents[3] / "omarchy" / "omarkets-launch"
    if repo.is_file() and os.access(repo, os.X_OK):
        return str(repo)
    return None


def launch_omarkets(destination: str, base_url: str = "") -> None:
    """Open or focus Omarkets at ``destination`` using the Phase 8 launcher.

    ``base_url`` is accepted so the caller can pass the client's base URL, but
    the launcher derives the port from its own configuration; the scheme/host is
    always loopback.
    """
    dest = (destination or "overview").strip().lower() or "overview"
    if dest not in VALID_DESTINATIONS:
        raise ValueError(f"unsupported destination: {destination!r}")

    launcher = _resolve_launcher()
    port = _port_from_url(base_url)
    if launcher is None:
        raise RuntimeError("Omarkets launcher not installed; run omarchy/install-plugin first.")

    argv = [launcher]
    if port:
        argv.append(str(port))
    # Overview and analysts both open the Today screen (the desk notes).
    if dest not in ("overview", "analysts"):
        argv.extend(["--section", dest])
    # Detached: the voice turn must not block on a window opening.
    subprocess.Popen(  # noqa: S603 - fixed argv, resolved launcher, no shell
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


def _port_from_url(base_url: str) -> int | None:
    if not base_url:
        return None
    from urllib.parse import urlparse

    try:
        port = urlparse(base_url).port
    except ValueError:
        return None
    if port is None or not (1 <= port <= 65535):
        return None
    return port


__all__ = ["VALID_DESTINATIONS", "launch_omarkets"]
