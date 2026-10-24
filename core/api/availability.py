"""Content-free timing evidence for local availability diagnosis."""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)


class RequestTimingMiddleware:
    """Time responses without buffering bodies or logging URLs/credentials.

    Route templates exclude identifiers and queries. Server-Timing measures time
    to response headers; completion logs include streaming/body-send time. Neither
    measures time queued before the ASGI handler began.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        slow_seconds: float = 1.0,
        clock: Callable[[], float] = time.perf_counter,
    ):
        self.app = app
        self.slow_seconds = slow_seconds
        self.clock = clock

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start = self.clock()
        size = 0
        status = 500
        # Event streams stay open by design; their duration is not slowness.
        event_stream = False

        async def timed_send(message: Message) -> None:
            nonlocal size, status, event_stream
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers", []))
                event_stream = any(
                    name.lower() == b"content-type" and value.startswith(b"text/event-stream")
                    for name, value in headers
                )
                headers.append(
                    (
                        b"server-timing",
                        (f"app;dur={(self.clock() - start) * 1000:.2f}").encode("ascii"),
                    )
                )
                message = {**message, "headers": headers}
            elif message["type"] == "http.response.body":
                size += len(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, receive, timed_send)
        finally:
            elapsed = self.clock() - start
            if elapsed >= self.slow_seconds and not event_stream:
                route = getattr(scope.get("route"), "path", "unmatched")
                logger.warning(
                    "slow_http route=%s status=%d duration_ms=%.2f bytes=%d",
                    route,
                    status,
                    elapsed * 1000,
                    size,
                )


@asynccontextmanager
async def monitor_event_loop(
    *,
    interval: float = 1.0,
    threshold: float = 0.25,
    stack_threshold: float = 2.0,
    sample_interval: float = 0.5,
):
    """Record stalls and the executing frame, without application content."""
    loop_thread = threading.get_ident()
    stop = threading.Event()
    last_progress = time.monotonic()

    def sample_stalled_loop() -> None:
        nonlocal last_progress
        last_report = 0.0
        while not stop.wait(sample_interval):
            now = time.monotonic()
            if now - last_progress < stack_threshold or now - last_report < 5.0:
                continue
            frame = sys._current_frames().get(loop_thread)
            if frame is None:
                continue
            frames = []
            while frame is not None and len(frames) < 8:
                code = frame.f_code
                frames.append(
                    f"{code.co_filename.rsplit('/', 1)[-1]}:{code.co_name}:{frame.f_lineno}"
                )
                frame = frame.f_back
            logger.warning(
                "event_loop_stack stalled_ms=%.2f frames=%s",
                (now - last_progress) * 1000,
                " <- ".join(frames),
            )
            last_report = now

    async def watch():
        nonlocal last_progress
        loop = asyncio.get_running_loop()
        while True:
            due = loop.time() + interval
            await asyncio.sleep(interval)
            last_progress = time.monotonic()
            lag = loop.time() - due
            if lag >= threshold:
                logger.warning("event_loop_lag lag_ms=%.2f", lag * 1000)

    sampler = threading.Thread(
        target=sample_stalled_loop, name="omarkets-loop-sampler", daemon=True
    )
    sampler.start()
    task = asyncio.create_task(watch(), name="omarkets-availability-monitor")
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        stop.set()
        sampler.join(timeout=1.0)
