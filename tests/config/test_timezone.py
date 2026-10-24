from pathlib import Path

import pytest

from core.config import timezone
from core.config.settings import Settings


@pytest.fixture
def system(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(timezone, "LOCALTIME", tmp_path / "localtime")
    monkeypatch.setattr(timezone, "TIMEZONE_FILE", tmp_path / "timezone")
    return tmp_path


def _zoneinfo_link(root: Path, name: str) -> None:
    target = root / "usr/share/zoneinfo" / name
    target.parent.mkdir(parents=True)
    target.write_bytes(b"")
    (root / "localtime").symlink_to(target)


def test_tz_env_wins(system: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _zoneinfo_link(system, "Europe/London")
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    assert timezone.system_timezone() == "Asia/Tokyo"


@pytest.mark.parametrize("value", ["", "EST5EDT,M3.2.0,M11.1.0", "Not/AZone", ":/etc/localtime"])
def test_invalid_tz_env_falls_through(system: Path, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    _zoneinfo_link(system, "Europe/London")
    monkeypatch.setenv("TZ", value)
    assert timezone.system_timezone() == "Europe/London"


def test_localtime_symlink(system: Path) -> None:
    _zoneinfo_link(system, "America/Argentina/Buenos_Aires")
    assert timezone.system_timezone() == "America/Argentina/Buenos_Aires"


def test_timezone_file_when_localtime_is_not_a_zoneinfo_link(system: Path) -> None:
    (system / "localtime").write_bytes(b"TZif")
    (system / "timezone").write_text("Australia/Sydney\n")
    assert timezone.system_timezone() == "Australia/Sydney"


def test_utc_when_nothing_is_usable(system: Path) -> None:
    (system / "timezone").write_text("garbage zone\n")
    assert timezone.system_timezone() == "UTC"


def test_settings_default_to_system_zone(system: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DESK_LOCAL_TIMEZONE", "AI_BUDGET_TIMEZONE"):
        monkeypatch.delenv(f"OMARKETS_{name}", raising=False)
        monkeypatch.delenv(f"MARKETOS_{name}", raising=False)
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    settings = Settings(_env_file=None)
    assert settings.desk_local_timezone == "Asia/Tokyo"
    assert settings.ai_budget_timezone == "Asia/Tokyo"


@pytest.mark.parametrize("prefix", ["OMARKETS_", "MARKETOS_"])
def test_explicit_setting_overrides_system_zone(
    system: Path, monkeypatch: pytest.MonkeyPatch, prefix: str
) -> None:
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    monkeypatch.setenv(f"{prefix}DESK_LOCAL_TIMEZONE", "Europe/London")
    monkeypatch.setenv(f"{prefix}AI_BUDGET_TIMEZONE", "America/Chicago")
    settings = Settings(_env_file=None)
    assert settings.desk_local_timezone == "Europe/London"
    assert settings.ai_budget_timezone == "America/Chicago"
