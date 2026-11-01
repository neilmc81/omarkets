"""Headless Quickshell regression for collector-age transport states."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(
    not shutil.which("quickshell") or not os.environ.get("WAYLAND_DISPLAY"),
    reason="requires Quickshell and a Wayland session",
)
def test_fresh_snapshot_does_not_flash_unavailable(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    env = {
        **os.environ,
        "OMARKETS_ROOT": os.environ.get("OMARKETS_QML_ROOT", str(root)),
        "XDG_STATE_HOME": str(tmp_path),
    }
    result = subprocess.run(
        [
            "quickshell",
            "-p",
            str(Path(__file__).with_name("TransportStateSmoke.qml")),
            "--no-color",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-1200:]


def test_current_qml_has_no_retired_projections() -> None:
    root = Path(__file__).resolve().parents[2]
    for name in ("Panel.qml", "OmarketsService.qml"):
        source = (root / "omarchy" / "omarkets" / name).read_text()
        assert "property var technical" not in source
        assert "root.technical" not in source
        assert "VWAP" not in source
        assert '"ATR (' not in source
        for retired in ("latest_analysis", "latestAnalysis", "latest_material", "eligibility",
                        "scenario"):
            assert retired not in source, (name, retired)


@pytest.mark.skipif(
    not shutil.which("quickshell") or not os.environ.get("WAYLAND_DISPLAY"),
    reason="requires Quickshell and a Wayland session",
)
@pytest.mark.parametrize("with_note", [True, False])
def test_desk_panel_renders_note_theme_and_opens_today(tmp_path: Path, with_note: bool) -> None:
    root = Path(__file__).resolve().parents[2]
    source_root = Path(os.environ.get("OMARKETS_QML_ROOT", str(root)))
    shared = Path("/usr/share/omarchy/shell")
    if not shared.is_dir():
        pytest.skip("requires installed Omarchy shared components")
    for name in ("Commons", "Ui", "services"):
        (tmp_path / name).symlink_to(shared / name, target_is_directory=True)
    shutil.copytree(source_root / "omarchy/omarkets", tmp_path / "Omarkets")
    shutil.copy2(Path(__file__).with_name("DeskPanelSmoke.qml"), tmp_path / "shell.qml")
    result = subprocess.run(
        ["quickshell", "-p", str(tmp_path / "shell.qml"), "--no-color"],
        env={**os.environ, "OMARKETS_DESK_NOTE": "1" if with_note else ""},
        capture_output=True, text=True, timeout=15, check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-4000:]
    assert "DESK_QML_OK" in output, output[-4000:]
    assert "TypeError" not in output and "ReferenceError" not in output, output[-4000:]
    # A bare `Color` resolves to undefined inside a shell Panel; theme fallbacks
    # must be `Commons.Color.*` so they never silently fail to bind.
    assert "Unable to assign [undefined]" not in output, output[-4000:]
