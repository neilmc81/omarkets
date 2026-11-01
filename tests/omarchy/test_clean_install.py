"""Phase 2 release cut: a clean install runs on the code defaults (desk:v9, NQ,
ES, YM, GOLD, WTI and DXY) with no systemd drop-in, and the installer says so."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SUMMARY = "Desk notes: NQ, ES, YM, GOLD, WTI, DXY on desk:v9"


def sandbox(tmp_path: Path) -> dict[str, str]:
    """A fresh user: empty config, stubbed host commands, a throwaway .env."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("systemctl", "omarchy", "omarchy-shell", "hyprctl"):
        stub = bin_dir / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{tmp_path}/calls.log"\n')
        stub.chmod(0o755)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("OMARKETS_", "MARKETOS_")) and key != "OPENROUTER_API_KEY"}
    return {**env, "PATH": f"{bin_dir}:{env['PATH']}", "HOME": str(tmp_path),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "OMARKETS_ENV_FILE": str(tmp_path / "test.env"),
            # The summary runs on this interpreter, so it is checked even
            # where the checkout has no .venv.
            "OMARKETS_PYTHON": sys.executable}


def install(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(ROOT / "omarchy/install-plugin"), "--no-prompt", *args],
                          env=env, capture_output=True, text=True, check=False)


def test_a_clean_install_writes_a_unit_with_no_settings_and_no_drop_in(tmp_path):
    env = sandbox(tmp_path)
    result = install(env)
    assert result.returncode == 0, result.stderr
    units = tmp_path / "config/systemd/user"
    unit = (units / "omarkets.service").read_text()
    assert f"ExecStart={ROOT}/.venv/bin/omarkets" in unit
    directives = [line for line in unit.splitlines() if not line.startswith("#")]
    assert not any(line.startswith(("Environment=", "EnvironmentFile=")) for line in directives)
    assert "OMARKETS_" not in unit and "@REPO@" not in unit
    assert sorted(p.name for p in units.iterdir()) == ["omarkets.service"]
    env_file = (tmp_path / "test.env").read_text()
    assert env_file == (ROOT / ".env.example").read_text()
    assert "systemctl --user enable --now omarkets.service" in (tmp_path / "calls.log").read_text()
    assert SUMMARY in result.stdout
    assert "omarketsctl config" in result.stdout


def test_dry_run_creates_nothing_and_names_the_defaults(tmp_path):
    env = sandbox(tmp_path)
    result = install(env, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "Dry run" in result.stdout
    assert not (tmp_path / "config").exists() and not (tmp_path / "test.env").exists()
    assert SUMMARY in result.stdout


def dry_run_with(tmp_path: Path, env_file: str) -> subprocess.CompletedProcess:
    env = sandbox(tmp_path)
    (tmp_path / "test.env").write_text(env_file)
    return install(env, "--dry-run")


@pytest.mark.parametrize(("env_file", "reason"), [
    # ES, YM, GOLD, WTI and DXY are code defaults, so a leftover v8 pin alone is unstartable.
    ("OMARKETS_DESK_PROMPT_VERSION=desk:v8\n", "desk:v8 has no prompt for them"),
    ('OMARKETS_DESK_PROMPT_VERSION=desk:v8\nOMARKETS_DESK_INSTRUMENTS=["NQ","ES"]\n',
     "desk:v8 has no prompt for it"),
    ('OMARKETS_DESK_PROMPT_VERSION=desk:v8\nOMARKETS_DESK_INSTRUMENTS=["NQ","YM"]\n',
     "remove YM"),
    ('OMARKETS_DESK_PROMPT_VERSION=desk:v8\nOMARKETS_DESK_INSTRUMENTS=["NQ","GOLD"]\n',
     "remove GOLD"),
    ('OMARKETS_DESK_PROMPT_VERSION=desk:v8\nOMARKETS_DESK_INSTRUMENTS=["NQ","WTI"]\n',
     "remove WTI"),
    ('OMARKETS_DESK_PROMPT_VERSION=desk:v8\nOMARKETS_DESK_INSTRUMENTS=["NQ","DXY"]\n',
     "remove DXY"),
    ('OMARKETS_DESK_INSTRUMENTS=["NQ","ES","YM","BTC"]\n', "desk:v9 has no prompt for it"),
    ('OMARKETS_DESK_INSTRUMENTS=["NQ","BTC"]\n', "remove BTC"),
    ('OMARKETS_DESK_INSTRUMENTS=["ES"]\n', "NQ is the desk's own instrument"),
])
def test_the_summary_reports_a_config_the_service_would_refuse(tmp_path, env_file, reason):
    result = dry_run_with(tmp_path, env_file)
    assert result.returncode == 0, result.stderr
    assert "Desk notes:" not in result.stdout
    assert "drop-ins not checked" not in result.stdout + result.stderr
    assert "service will refuse to start" in result.stderr and reason in result.stderr


@pytest.mark.parametrize(("env_file", "line"), [
    ('OMARKETS_DESK_PROMPT_VERSION=desk:v8\nOMARKETS_DESK_INSTRUMENTS=["NQ"]\n',
     "Desk notes: NQ on desk:v8 (code defaults and .env; systemd drop-ins not checked)"),
    ('OMARKETS_DESK_INSTRUMENTS=["NQ"]\n',
     "Desk notes: NQ on desk:v9 (code defaults and .env; systemd drop-ins not checked)"),
    ('OMARKETS_DESK_INSTRUMENTS=["NQ","ES"]\n',
     "Desk notes: NQ, ES on desk:v9 (code defaults and .env; systemd drop-ins not checked)"),
    ("OMARKETS_DESK_ENABLED=false\n", "Desk notes: off"),
])
def test_the_summary_names_a_startable_override(tmp_path, env_file, line):
    result = dry_run_with(tmp_path, env_file)
    assert result.returncode == 0, result.stderr
    assert line in result.stdout and "refuse" not in result.stderr


def test_the_summary_says_when_it_could_not_check(tmp_path):
    env = {**sandbox(tmp_path), "OMARKETS_PYTHON": str(tmp_path / "missing/python")}
    result = install(env, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "Desk notes: not checked" in result.stderr
    assert "drop-ins not checked" not in result.stdout


def test_an_unimportable_venv_is_not_reported_as_a_settings_refusal(tmp_path):
    # -I -S drop PYTHONPATH and site-packages, so `core` cannot be imported:
    # a half-synced venv.
    broken = tmp_path / "python"
    broken.write_text(f"#!/bin/sh\nexec {sys.executable} -I -S \"$@\"\n")
    broken.chmod(0o755)
    env = {**sandbox(tmp_path), "OMARKETS_PYTHON": str(broken)}
    result = install(env, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "Desk notes: not checked" in result.stderr and "ModuleNotFoundError" in result.stderr
    assert "refuse" not in result.stderr and "Desk notes:" not in result.stdout


def test_the_smoke_script_passes_on_the_code_defaults(tmp_path):
    env = sandbox(tmp_path)
    result = subprocess.run(["bash", str(ROOT / "omarchy/smoke-defaults")], env=env,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAIL" not in result.stdout and "all checks passed" in result.stdout


def test_the_smoke_script_refuses_unknown_arguments():
    result = subprocess.run(["bash", str(ROOT / "omarchy/smoke-defaults"), "--bogus"],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 2 and "unknown argument" in result.stderr
