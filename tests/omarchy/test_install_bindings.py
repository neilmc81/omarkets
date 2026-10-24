"""Exercise only install-plugin's hotkey stage against temporary Lua files."""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MARKER = "-- [Omarkets] native integration\n"
LEGACY = 'o.bind("SUPER + SHIFT + M", "Omarkets popup", "omarchy-shell shell toggle omarkets")\n'
CURRENT = LEGACY.replace("SUPER + SHIFT + M", "SUPER + CTRL + M")
MUSIC = 'o.bind("SUPER + SHIFT + M", "Music", "spotify")\n'
OTHER = 'o.bind("SUPER + B", "Custom", "btop")\n'


def run_stage(tmp_path: Path, source: str) -> tuple[subprocess.CompletedProcess, Path]:
    config = tmp_path / "config"
    config.joinpath("hypr").mkdir(parents=True, exist_ok=True)
    bindings = config / "hypr/bindings.lua"
    bindings.write_text(source)
    installer = (ROOT / "omarchy/install-plugin").read_text()
    stage = installer.split("# --- 5. hotkeys", 1)[1].split("# Ask the running shell", 1)[0]
    stage = stage[stage.index("if (( bindings )); then") :]
    # Functions intercept reload/backup; no host commands or install stages run.
    script = (
        """set -euo pipefail
config_home=$1
repo=$2
port=8765
bindings=1
backup() { cp -a -- "$1" "$1.test-backup"; }
hyprctl() { return 0; }
"""
        + stage
    )
    result = subprocess.run(
        ["bash", "-c", script, "test", str(config), str(ROOT)],
        capture_output=True,
        text=True,
        check=False,
    )
    return result, bindings


def test_fresh_install_preserves_music_and_is_idempotent(tmp_path: Path) -> None:
    result, path = run_stage(tmp_path, MUSIC + OTHER)
    assert result.returncode == 0, result.stderr
    first = path.read_text()
    assert first.startswith(MUSIC + OTHER)
    assert CURRENT in first
    assert LEGACY not in first
    result, path = run_stage(tmp_path, first)
    assert result.returncode == 0, result.stderr
    assert path.read_text() == first


def test_legacy_migration_changes_only_owned_popup_and_backs_up(tmp_path: Path) -> None:
    original = MUSIC + MARKER + LEGACY + OTHER
    result, path = run_stage(tmp_path, original)
    assert result.returncode == 0, result.stderr
    assert path.read_text() == original.replace(LEGACY, CURRENT)
    backups = list(path.parent.glob("bindings.lua.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == original
    result, path = run_stage(tmp_path, path.read_text())
    assert result.returncode == 0, result.stderr
    assert len(list(path.parent.glob("bindings.lua.bak-*"))) == 1


def test_migration_refuses_occupied_reordered_shortcut(tmp_path: Path) -> None:
    original = MUSIC + 'o.bind("CTRL + SUPER + M", "Custom", "custom")\n' + MARKER + LEGACY
    result, path = run_stage(tmp_path, original)
    assert result.returncode != 0
    assert "already bound" in result.stderr
    assert path.read_text() == original
    assert not list(path.parent.glob("bindings.lua.bak-*"))


def test_unmarked_legacy_popup_is_preserved(tmp_path: Path) -> None:
    original = LEGACY + MARKER + CURRENT + OTHER
    result, path = run_stage(tmp_path, original)
    assert result.returncode == 0, result.stderr
    assert path.read_text() == original


@pytest.mark.parametrize(
    "shortcut", ["SUPER + CTRL + M", "CTRL + SUPER + M", "SUPER + ALT + M", "SUPER + M"]
)
def test_fresh_install_refuses_existing_popup_shortcut(tmp_path: Path, shortcut: str) -> None:
    original = MUSIC + f'o.bind("{shortcut}", "Custom", "custom")\n'
    result, path = run_stage(tmp_path, original)
    assert result.returncode == 0, result.stderr
    assert "not adding hotkeys" in result.stderr
    assert path.read_text() == original
