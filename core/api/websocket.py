"""WebSocket endpoint.

Publishes complete market-state snapshots as they are produced. The core keeps
running with zero connected clients; clients receive the latest snapshot
immediately on connect and then one message per update.

Message envelope::

    {"type": "state", "data": { ...MarketState... }}

The envelope exists so additional message kinds can be added later without
breaking clients.

The handler waits on the state subscription and on the client transport at the
same time. Waiting on both matters during server shutdown: uvicorn closes
connections *before* it emits the lifespan shutdown event, so a handler blocked
only on the subscription would delay shutdown until the next market update — or
hang indefinitely with a quiet feed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from core.services.economics import EconomicService
from core.services.market_state import (
    MarketStateService,
    SubscriptionClosed,
    SubscriptionLike,
)
from core.services.news import NewsService

logger = logging.getLogger(__name__)

router = APIRouter()


async def _next_state_or_disconnect(websocket: WebSocket, subscription: SubscriptionLike):
    """Return the next state, or ``None`` once the client transport closes."""
    state_task = asyncio.ensure_future(subscription.get())
    receive_task = asyncio.ensure_future(websocket.receive())
    try:
        done, _pending = await asyncio.wait(
            {state_task, receive_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        for task in (state_task, receive_task):
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    if state_task in done:
        # A state result takes priority so no update is dropped, even if the
        # client frame also completed.
        return state_task.result()

    # The transport finished first: a disconnect, or an inbound client frame.
    # Either way this handler only consumes state, so stop.
    return None


@router.websocket("/ws/market")
async def market_socket(
    websocket: WebSocket,
    topics: str | None = Query(default=None),
) -> None:
    """Stream market-state snapshots, and optionally economic/news events.

    Without extra topics: one ``{"type": "state", ...}`` snapshot per update.
    ``?topics=macro`` adds coalesced economic messages; ``?topics=news`` adds
    coalesced news-event invalidations. Topics may be combined with a comma,
    e.g. ``?topics=macro,news``.
    """
    await websocket.accept()

    service: MarketStateService | None = getattr(websocket.app.state, "service", None)
    if service is None:  # pragma: no cover - misconfiguration
        await websocket.close(code=1011, reason="state service unavailable")
        return

    requested = {topic.strip() for topic in topics.split(",")} if topics else set()

    economic: EconomicService | None = getattr(websocket.app.state, "economics", None)
    wants_macro = "macro" in requested
    if wants_macro and (economic is None or economic.mode.value == "disabled"):
        await websocket.send_json(
            {
                "type": "error",
                "data": {"detail": "macro topic unavailable: economics disabled"},
            }
        )
        await websocket.close(code=1011, reason="macro topic unavailable")
        return

    news: NewsService | None = getattr(websocket.app.state, "news", None)
    wants_news = "news" in requested
    if wants_news and (news is None or not news.enabled):
        await websocket.send_json(
            {
                "type": "error",
                "data": {"detail": "news topic unavailable: news service disabled"},
            }
        )
        await websocket.close(code=1011, reason="news topic unavailable")
        return

    client_id, subscription = service.subscribe()
    macro_mailbox = economic.subscribe() if wants_macro and economic is not None else None
    news_mailbox = news.subscribe() if wants_news and news is not None else None

    tasks: list[asyncio.Task] = []
    try:
        if any(
            mailbox is not None
            for mailbox in (
                macro_mailbox,
                news_mailbox,
            )
        ):
            state_task = asyncio.create_task(subscription.get())
            receive_task = asyncio.create_task(websocket.receive())
            tasks = [state_task, receive_task]
            # Each extra topic has its own read task, replaced after each message.
            mailbox_tasks: dict[asyncio.Task, object] = {}
            for mailbox in (
                macro_mailbox,
                news_mailbox,
            ):
                if mailbox is not None:
                    task = asyncio.create_task(mailbox.get())
                    tasks.append(task)
                    mailbox_tasks[task] = mailbox
            while True:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                if receive_task in done:
                    receive_task.result()
                    return
                if state_task in done:
                    state = state_task.result()
                    await websocket.send_json(
                        {"type": "state", "data": state.model_dump(mode="json")}
                    )
                    tasks.remove(state_task)
                    state_task = asyncio.create_task(subscription.get())
                    tasks.append(state_task)
                for task in list(mailbox_tasks):
                    if task not in done:
                        continue
                    mailbox = mailbox_tasks.pop(task)
                    message = task.result()
                    payload = (
                        message if isinstance(message, dict) else message.model_dump(mode="json")
                    )
                    await websocket.send_json(payload)
                    tasks.remove(task)
                    replacement = asyncio.create_task(mailbox.get())
                    tasks.append(replacement)
                    mailbox_tasks[replacement] = mailbox
        else:
            while True:
                state = await _next_state_or_disconnect(websocket, subscription)
                if state is None:
                    logger.debug("websocket client %d transport closed", client_id)
                    return
                await websocket.send_json({"type": "state", "data": state.model_dump(mode="json")})
    except SubscriptionClosed:
        logger.debug("websocket client %d released on shutdown", client_id)
        with contextlib.suppress(Exception):
            await websocket.close(code=1001, reason="server shutting down")
    except WebSocketDisconnect:
        logger.debug("websocket client %d disconnected", client_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("websocket client %d failed", client_id)
    finally:
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        service.unsubscribe(client_id)
        if macro_mailbox is not None and economic is not None:
            economic.unsubscribe(macro_mailbox)
        if news_mailbox is not None and news is not None:
            news.unsubscribe(news_mailbox)
