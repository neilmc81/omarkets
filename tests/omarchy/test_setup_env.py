"""omarchy/setup-env: create .env and store the OpenRouter key safely.

Every test works on a temporary .env with fake keys; nothing touches the
checkout's real .env.
"""

import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "omarchy/setup-env"
EXAMPLE = "# Omarkets settings\nOPENROUTER_API_KEY=\n# OMARKETS_FRED_API_KEY=\n"
FAKE = "sk-or-v1-0000fakekeyfortests0000"


def run(
    tmp_path: Path,
    *args: str,
    stdin: str | None = None,
    force_prompt: bool = False,
    env_key: str | None = None,
) -> subprocess.CompletedProcess:
    example = tmp_path / "example.env"
    if not example.exists():
        example.write_text(EXAMPLE)
    env = {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"}
    env["OMARKETS_ENV_FILE"] = str(tmp_path / ".env")
    env["OMARKETS_ENV_EXAMPLE"] = str(example)
    if force_prompt:
        env["OMARKETS_SETUP_ENV_FORCE_PROMPT"] = "1"
    if env_key is not None:
        env["OPENROUTER_API_KEY"] = env_key
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        input=stdin if stdin is not None else "",
        capture_output=True,
        text=True,
        check=False,
    )


def key_lines(path: Path) -> list[str]:
    return [
        line for line in path.read_text().splitlines() if line.startswith("OPENROUTER_API_KEY=")
    ]


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_no_tty_creates_env_and_skips(tmp_path: Path) -> None:
    r = run(tmp_path)
    assert r.returncode == 0, r.stderr
    env = tmp_path / ".env"
    assert env.read_text() == EXAMPLE
    assert mode(env) == 0o600
    assert "Paste your" not in r.stderr
    assert "To add it later" in r.stdout


def test_no_prompt_flag_skips_even_with_input(tmp_path: Path) -> None:
    r = run(tmp_path, "--no-prompt", stdin=FAKE + "\n", force_prompt=True)
    assert r.returncode == 0
    assert key_lines(tmp_path / ".env") == ["OPENROUTER_API_KEY="]
    assert "Paste your" not in r.stderr


def test_prompt_saves_trimmed_key_without_echo(tmp_path: Path) -> None:
    r = run(tmp_path, stdin=f"  {FAKE} \n", force_prompt=True)
    assert r.returncode == 0, r.stderr
    env = tmp_path / ".env"
    assert key_lines(env) == [f"OPENROUTER_API_KEY={FAKE}"]
    assert mode(env) == 0o600
    assert "Paste your OpenRouter API key" in r.stderr
    assert FAKE not in r.stdout and FAKE not in r.stderr
    # The rest of the file is kept as it was.
    assert "# OMARKETS_FRED_API_KEY=" in env.read_text()


def test_enter_skips(tmp_path: Path) -> None:
    r = run(tmp_path, stdin="\n", force_prompt=True)
    assert r.returncode == 0
    assert key_lines(tmp_path / ".env") == ["OPENROUTER_API_KEY="]
    assert "Skipped" in r.stdout


def test_existing_key_is_never_replaced(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(f"OPENROUTER_API_KEY={FAKE}\n")
    env.chmod(0o644)
    r = run(tmp_path, stdin="sk-or-v1-other\n", force_prompt=True)
    assert r.returncode == 0
    assert key_lines(env) == [f"OPENROUTER_API_KEY={FAKE}"]
    assert "already set" in r.stdout
    assert "Paste your" not in r.stderr
    assert mode(env) == 0o600


def test_key_from_environment(tmp_path: Path) -> None:
    r = run(tmp_path, env_key=FAKE)
    assert r.returncode == 0
    assert key_lines(tmp_path / ".env") == [f"OPENROUTER_API_KEY={FAKE}"]
    assert FAKE not in r.stdout + r.stderr


def test_unusual_characters_are_refused(tmp_path: Path) -> None:
    hostile = 'sk-or-abc"; touch pwned; echo "\\n$HOME/&|'
    r = run(tmp_path, stdin=hostile + "\n", force_prompt=True)
    assert r.returncode == 0
    assert key_lines(tmp_path / ".env") == ["OPENROUTER_API_KEY="]
    assert not (tmp_path / "pwned").exists()
    assert "not saved" in r.stderr


def test_non_openrouter_looking_key_warns_but_saves(tmp_path: Path) -> None:
    r = run(tmp_path, stdin="abc123-not-openrouter\n", force_prompt=True)
    assert r.returncode == 0
    assert key_lines(tmp_path / ".env") == ["OPENROUTER_API_KEY=abc123-not-openrouter"]
    assert "Warning" in r.stderr


def test_duplicate_key_lines_collapse_to_one(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("A=1\nOPENROUTER_API_KEY=\nB=2\nOPENROUTER_API_KEY=\n")
    r = run(tmp_path, stdin=FAKE + "\n", force_prompt=True)
    assert r.returncode == 0
    assert env.read_text() == f"A=1\nOPENROUTER_API_KEY={FAKE}\nB=2\n"


def test_missing_key_line_is_appended(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("A=1\n")
    r = run(tmp_path, stdin=FAKE + "\n", force_prompt=True)
    assert r.returncode == 0
    assert env.read_text() == f"A=1\nOPENROUTER_API_KEY={FAKE}\n"
