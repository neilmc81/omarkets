"""Omarchy palette reading, fallback, change detection and the theme API."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api import theme as theme_api
from core.theme.omarchy import FALLBACK, ROLES, OmarchyTheme, parse_palette

OSAKA = """mode = "dark"

accent = "#509475"
selection = "#32473B"
muted = "#53685B"

background = "#111c18"
foreground = "#C1C497"
bright_foreground = "#F7E8B2"

red = "#FF5345"
yellow = "#459451"
orange = "#a2734b"
green = "#549e6a"
cyan = "#2DD5B7"
blue = "#509475"
magenta = "#D2689C"
"""

WHITE = """mode = "light"
accent = "#6e6e6e"
background = "#ffffff"
foreground = "#000000"
red = "#2a2a2a"
"""


def install(state: Path, name: str, colors: str) -> Path:
    """Mimic ``omarchy theme set``: build next-theme, rm -rf current, mv."""
    current = state / "omarchy" / "current"
    current.mkdir(parents=True, exist_ok=True)
    staging = current / "next-theme"
    staging.mkdir()
    (staging / "colors.toml").write_text(colors)
    shutil.rmtree(current / "theme", ignore_errors=True)
    staging.rename(current / "theme")
    (current / "theme.name").write_text(name + "\n")
    return current / "theme"


def test_osaka_palette_maps_every_role() -> None:
    palette = parse_palette(OSAKA, "osaka-jade")
    assert palette.source == "omarchy"
    assert palette.mode == "dark"
    assert set(palette.colors) == set(ROLES)
    assert palette.colors["background"] == "#111c18"
    assert palette.colors["foreground"] == "#c1c497"
    assert palette.colors["green"] == "#549e6a"
    assert palette.colors["orange"] == "#a2734b"
    assert palette.colors["accent"] == "#509475"


def test_light_theme_and_missing_roles_fall_back_role_by_role() -> None:
    palette = parse_palette(WHITE, "white")
    assert palette.mode == "light"
    assert palette.colors["background"] == "#ffffff"
    # Not in the file: derived from what is there, else the built-in value.
    assert palette.colors["bright_foreground"] == "#000000"
    assert palette.colors["muted"] == "#000000"
    assert palette.colors["green"] == FALLBACK["green"]


def test_mode_is_inferred_when_the_theme_omits_it() -> None:
    palette = parse_palette('background = "#fafafa"\nforeground = "#111111"\n', None)
    assert palette.mode == "light"


def test_legacy_terminal_colours_are_used_when_named_roles_are_missing() -> None:
    palette = parse_palette(
        'color0 = "#1a1b26"\ncolor1 = "#f7768e"\ncolor2 = "#9ece6a"\n'
        'color4 = "#7aa2f7"\ncolor7 = "#a9b1d6"\n', None)
    assert palette.colors["background"] == "#1a1b26"
    assert palette.colors["foreground"] == "#a9b1d6"
    assert palette.colors["red"] == "#f7768e"
    assert palette.colors["green"] == "#9ece6a"
    assert palette.colors["accent"] == "#7aa2f7"


def test_invalid_hex_values_are_ignored() -> None:
    palette = parse_palette(
        'background = "#111c18"\nforeground = "#C1C497"\nred = "url(evil)"\n', None)
    assert palette.colors["red"] == FALLBACK["red"]


def test_without_omarchy_the_built_in_palette_is_served(tmp_path: Path) -> None:
    palette = OmarchyTheme(tmp_path / "missing" / "theme").current()
    assert palette.source == "fallback"
    assert palette.colors == FALLBACK
    assert palette.detail


@pytest.mark.parametrize("content", ["not = [valid", 'background = "#111c18"\n', "\xff"])
def test_unreadable_theme_falls_back(tmp_path: Path, content: str) -> None:
    theme_dir = install(tmp_path, "broken", "")
    (theme_dir / "colors.toml").write_bytes(content.encode("latin-1"))
    assert OmarchyTheme(theme_dir).current().source == "fallback"


def test_initial_load_reads_the_active_theme(tmp_path: Path) -> None:
    theme_dir = install(tmp_path, "osaka-jade", OSAKA)
    palette = OmarchyTheme(theme_dir).current()
    assert palette.source == "omarchy"
    assert palette.name == "osaka-jade"
    assert palette.colors["background"] == "#111c18"


def test_theme_switch_is_detected_through_the_directory_swap(tmp_path: Path) -> None:
    theme_dir = install(tmp_path, "osaka-jade", OSAKA)
    reader = OmarchyTheme(theme_dir)
    first = reader.current()
    assert reader.current() is first  # unchanged file is not re-parsed
    install(tmp_path, "white", WHITE)
    second = reader.current()
    assert second.version != first.version
    assert second.name == "white"
    assert second.mode == "light"
    # Switching back restores the same version (content-addressed).
    install(tmp_path, "osaka-jade", OSAKA)
    assert reader.current().version == first.version


async def test_hook_notification_wakes_waiting_streams_at_once(tmp_path: Path) -> None:
    theme_dir = install(tmp_path, "osaka-jade", OSAKA)
    reader = OmarchyTheme(theme_dir)
    first = reader.current()
    waiter = asyncio.create_task(reader.wait_for_change(first.version, within=30))
    await asyncio.sleep(0.05)
    install(tmp_path, "white", WHITE)
    await reader.notify()
    changed = await asyncio.wait_for(waiter, timeout=0.5)
    assert changed.name == "white"


async def test_change_is_noticed_without_the_hook(tmp_path: Path) -> None:
    theme_dir = install(tmp_path, "osaka-jade", OSAKA)
    reader = OmarchyTheme(theme_dir)
    first = reader.current()
    waiter = asyncio.create_task(reader.wait_for_change(first.version, within=30))
    await asyncio.sleep(0.05)
    install(tmp_path, "white", WHITE)
    changed = await asyncio.wait_for(waiter, timeout=2.5)
    assert changed.name == "white"


async def test_wait_returns_unchanged_after_timeout(tmp_path: Path) -> None:
    reader = OmarchyTheme(install(tmp_path, "osaka-jade", OSAKA))
    first = reader.current()
    assert (await reader.wait_for_change(first.version, within=0.2)).version == first.version


def test_theme_api_get_and_reload(tmp_path: Path) -> None:
    theme_dir = install(tmp_path, "osaka-jade", OSAKA)
    app = FastAPI()
    app.state.theme = OmarchyTheme(theme_dir)
    app.include_router(theme_api.router)
    client = TestClient(app)
    body = client.get("/api/v1/theme").json()
    assert body["source"] == "omarchy"
    assert body["colors"]["background"] == "#111c18"
    install(tmp_path, "white", WHITE)
    reloaded = client.post("/api/v1/theme/reload").json()
    assert reloaded["name"] == "white"
    assert reloaded["version"] != body["version"]


async def test_close_ends_waiting_streams_promptly(tmp_path: Path) -> None:
    reader = OmarchyTheme(install(tmp_path, "osaka-jade", OSAKA))
    first = reader.current()
    waiter = asyncio.create_task(reader.wait_for_change(first.version, within=30))
    await asyncio.sleep(0.05)
    reader.close()
    assert (await asyncio.wait_for(waiter, timeout=0.5)).version == first.version
    assert reader.closed
