"""Regression coverage for API service shutdown order."""

from __future__ import annotations

import pytest

from core.api.app import create_app


class _Service:
    def __init__(self, name: str, events: list[str], *, fail_stop: bool = False) -> None:
        self.name = name
        self.events = events
        self.fail_stop = fail_stop

    async def start(self) -> None:
        self.events.append(f"start:{self.name}")

    async def stop(self) -> None:
        self.events.append(f"stop:{self.name}")
        if self.fail_stop:
            raise RuntimeError("shutdown failed")


@pytest.mark.asyncio
async def test_lifespan_stops_desk_before_its_sources() -> None:
    events: list[str] = []
    app = create_app(
        service=_Service("market", events),
        economic_service=_Service("economics", events),
        news_service=_Service("news", events),
        desk_service=_Service("desk", events),
    )

    async with app.router.lifespan_context(app):
        pass

    assert events == [
        "start:market",
        "start:economics",
        "start:news",
        "start:desk",
        "stop:desk",
        "stop:news",
        "stop:economics",
        "stop:market",
    ]


@pytest.mark.asyncio
async def test_lifespan_keeps_stopping_sources_if_desk_stop_fails() -> None:
    events: list[str] = []
    app = create_app(
        service=_Service("market", events),
        economic_service=_Service("economics", events),
        news_service=_Service("news", events),
        desk_service=_Service("desk", events, fail_stop=True),
    )

    with pytest.raises(RuntimeError, match="shutdown failed"):
        async with app.router.lifespan_context(app):
            pass

    assert events[-4:] == [
        "stop:desk",
        "stop:news",
        "stop:economics",
        "stop:market",
    ]
