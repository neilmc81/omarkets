"""Theme API: the active Omarchy palette, a change stream, and the hook's reload."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from core.theme.omarchy import OmarchyTheme, ThemePalette

router = APIRouter(prefix="/api/v1", tags=["theme"])

#: A comment line is sent this often so idle connections are not dropped.
KEEPALIVE_SECONDS = 15.0


def _theme(request: Request) -> OmarchyTheme:
    theme = getattr(request.app.state, "theme", None)
    if theme is None:
        theme = OmarchyTheme()
        request.app.state.theme = theme
    return theme


@router.get("/theme", response_model=ThemePalette)
async def theme_palette(request: Request) -> ThemePalette:
    return _theme(request).current()


@router.post("/theme/reload", response_model=ThemePalette)
async def theme_reload(request: Request) -> ThemePalette:
    """Called by the Omarchy ``theme-set`` hook after a theme switch."""
    return await _theme(request).notify()


@router.get("/theme/stream")
async def theme_stream(request: Request) -> StreamingResponse:
    """Server-sent events: the palette now, then again on every change."""
    theme = _theme(request)

    async def events() -> AsyncIterator[str]:
        palette = theme.current()
        yield f"event: theme\ndata: {palette.model_dump_json()}\n\n"
        while not theme.closed and not await request.is_disconnected():
            latest = await theme.wait_for_change(palette.version, KEEPALIVE_SECONDS)
            if theme.closed:
                # Server shutting down: end the stream; EventSource reconnects.
                return
            if latest.version == palette.version:
                yield ": keepalive\n\n"
                continue
            palette = latest
            yield f"event: theme\ndata: {palette.model_dump_json()}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})
