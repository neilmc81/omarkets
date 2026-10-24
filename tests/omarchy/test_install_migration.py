"""Run install-plugin's MarketOS -> Omarkets migration in a sandboxed config."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
OLD_BLOCK = (
    "-- [MarketOS] native integration\n"
    "-- SUPER+M opens/focuses the workstation; SUPER+CTRL+M toggles the popup;\n"
    "-- SUPER+ALT+M opens the economic calendar. Add --section to deep-link.\n"
    'o.bind("SUPER + CTRL + M", "MarketOS popup", "omarchy-shell shell toggle marketos")\n'
    'o.bind("SUPER + M", "MarketOS", "/repo/omarchy/marketos-launch 8765")\n'
    'o.bind("SUPER + ALT + M", "MarketOS calendar", '
    '"/repo/omarchy/marketos-launch 8765 --section calendar")\n'
)
NEW_BLOCK = (
    "-- [Omarkets] native integration\n"
    "-- SUPER+M opens/focuses the workstation; SUPER+CTRL+M toggles the popup;\n"
    "-- SUPER+ALT+M opens the economic calendar. Add --section to deep-link.\n"
    'o.bind("SUPER + CTRL + M", "Omarkets popup", "omarchy-shell shell toggle omarkets")\n'
    'o.bind("SUPER + M", "Omarkets", "/repo/omarchy/omarkets-launch 8765")\n'
    'o.bind("SUPER + ALT + M", "Omarkets calendar", '
    '"/repo/omarchy/omarkets-launch 8765 --section calendar")\n'
)
OTHER = 'o.bind("SUPER + B", "Custom", "btop")\n'
SHELL_JSON = (
    '{\n  "bar": {\n    "layout": {\n      "right": [\n'
    '        {\n          "id": "omarchy.tray"\n        },\n'
    '        {\n          "id": "marketos",\n          "apiPort": "8765"\n        },\n'
    '        {\n          "id": "openrouter.cost"\n        }\n      ]\n    }\n  }\n}\n'
)


def sandbox(tmp_path: Path) -> tuple[dict[str, str], Path]:
    config = tmp_path / "config"
    plugins = config / "omarchy/plugins"
    plugins.mkdir(parents=True)
    (plugins / "marketos").symlink_to(tmp_path)
    units = config / "systemd/user"
    (units / "marketos.service.d").mkdir(parents=True)
    (units / "marketos.service").write_text("[Service]\n")
    (units / "marketos.service.d/50-desk.conf").write_text("[Service]\n")
    (config / "omarchy/shell.json").write_text(SHELL_JSON)
    hooks = config / "omarchy/hooks/theme-set.d"
    hooks.mkdir(parents=True)
    (hooks / "theme-set-marketos").write_text("#!/bin/bash\n")
    (hooks / "theme-set-marketos").chmod(0o755)
    (config / "hypr").mkdir()
    (config / "hypr/bindings.lua").write_text(OTHER + OLD_BLOCK)
    # Stub every host command the installer may call; each one logs and succeeds.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("systemctl", "omarchy", "omarchy-shell", "hyprctl"):
        stub = bin_dir / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{tmp_path}/calls.log"\n')
        stub.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(config),
        "XDG_DATA_HOME": str(tmp_path / "data"),
    }
    return env, config


def install(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(ROOT / "omarchy/install-plugin"), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_dry_run_changes_nothing(tmp_path: Path) -> None:
    env, config = sandbox(tmp_path)
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    result = install(env, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "would run:" in result.stdout
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before
    assert (config / "hypr/bindings.lua").read_text() == OTHER + OLD_BLOCK
    assert not (tmp_path / "calls.log").exists()


def test_migrates_and_is_idempotent(tmp_path: Path) -> None:
    env, config = sandbox(tmp_path)
    result = install(env)
    assert result.returncode == 0, result.stderr
    plugins = config / "omarchy/plugins"
    units = config / "systemd/user"
    assert not (plugins / "marketos").exists()
    assert (plugins / "omarkets").resolve() == ROOT / "omarchy/omarkets"
    assert list(plugins.glob("marketos.bak-*"))
    assert not (units / "marketos.service").exists()
    assert list(units.glob("marketos.service.bak-*"))
    assert (units / "omarkets.service.d/50-desk.conf").exists()
    assert (units / "marketos.service.d/50-desk.conf").exists()
    unit = (units / "omarkets.service").read_text()
    assert f"ExecStart={ROOT}/.venv/bin/omarkets" in unit
    assert "@REPO@" not in unit
    assert (config / "hypr/bindings.lua").read_text() == OTHER + NEW_BLOCK
    assert list((config / "hypr").glob("bindings.lua.bak-*"))
    hooks = config / "omarchy/hooks"
    assert not (hooks / "theme-set.d/theme-set-marketos").exists()
    new_hook = hooks / "theme-set.d/theme-set-omarkets"
    assert new_hook.read_text() == (ROOT / "omarchy/hooks/theme-set-omarkets").read_text()
    assert os.access(new_hook, os.X_OK)
    # The backup sits outside theme-set.d so Omarchy never runs it.
    assert list((hooks / "omarkets-migration-backup").glob("theme-set-marketos.bak-*"))
    assert [p.name for p in (hooks / "theme-set.d").iterdir()] == ["theme-set-omarkets"]
    assert (config / "omarchy/shell.json").read_text() == SHELL_JSON.replace(
        '"marketos"', '"omarkets"'
    )
    assert list((config / "omarchy").glob("shell.json.bak-*"))
    # The desk zone follows the system now, so no time-zone drop-in is written.
    assert not (units / "omarkets.service.d/60-desk-timezone.conf").exists()
    calls = (tmp_path / "calls.log").read_text()
    assert "systemctl --user disable --now marketos.service" in calls

    snapshot = sorted(p.relative_to(tmp_path) for p in config.rglob("*"))
    again = install(env)
    assert again.returncode == 0, again.stderr
    assert "Migrating" not in again.stdout
    assert sorted(p.relative_to(tmp_path) for p in config.rglob("*")) == snapshot


def test_existing_desk_timezone_is_left_alone(tmp_path: Path) -> None:
    env, config = sandbox(tmp_path)
    dropins = config / "systemd/user/marketos.service.d"
    (dropins / "50-desk.conf").write_text(
        "[Service]\nEnvironment=MARKETOS_DESK_LOCAL_TIMEZONE=Europe/London\n"
    )
    result = install(env)
    assert result.returncode == 0, result.stderr
    assert not (config / "systemd/user/omarkets.service.d/60-desk-timezone.conf").exists()


def test_fresh_install_adds_no_timezone_dropin(tmp_path: Path) -> None:
    env, config = sandbox(tmp_path)
    units = config / "systemd/user"
    (units / "marketos.service").unlink()
    for child in (units / "marketos.service.d").iterdir():
        child.unlink()
    (units / "marketos.service.d").rmdir()
    result = install(env)
    assert result.returncode == 0, result.stderr
    assert not (units / "omarkets.service.d").exists()


def test_marketos_launch_shim_execs_new_launcher() -> None:
    shim = ROOT / "omarchy/marketos-launch"
    if not shim.exists():
        pytest.skip("compatibility shim is not shipped in releases")
    assert "DEPRECATED" in shim.read_text() and os.access(shim, os.X_OK)
    result = subprocess.run([str(shim), "--help"], capture_output=True, text=True, check=False)
    assert result.returncode == 0 and "omarkets-launch" in result.stdout
