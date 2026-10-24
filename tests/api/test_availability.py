"""Availability telemetry stays bounded and never records request content."""
import asyncio
import logging
import time
from types import SimpleNamespace

import pytest

from core.api.availability import RequestTimingMiddleware, monitor_event_loop


@pytest.mark.asyncio
async def test_timing_streams_body_and_logs_only_route_template(caplog):
    clock = [0.0]
    messages = []

    async def app(scope, receive, send):
        scope["route"] = SimpleNamespace(path="/events/{event_id}")
        clock[0] = 1.25
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"private-content",
                    "more_body": True})
        await send({"type": "http.response.body", "body": b"!"})

    async def receive():
        return {"type": "http.request"}

    async def send(message):
        messages.append(message)

    with caplog.at_level(logging.WARNING):
        await RequestTimingMiddleware(app, clock=lambda: clock[0])(
            {"type": "http", "path": "/events/private-id",
             "query_string": b"token=private-token"}, receive, send,
        )
    assert (b"server-timing", b"app;dur=1250.00") in messages[0]["headers"]
    assert messages[1]["body"] == b"private-content"
    assert "bytes=16" in caplog.text
    assert "/events/{event_id}" in caplog.text
    assert "private" not in caplog.text


@pytest.mark.asyncio
async def test_event_loop_stall_is_observed_and_monitor_stops(caplog):
    with caplog.at_level(logging.WARNING):
        async with monitor_event_loop(interval=0.005, threshold=0.01):
            await asyncio.sleep(0)  # Let the observer establish its deadline.
            end = time.perf_counter() + 0.025
            while time.perf_counter() < end:
                pass  # Deliberately reproduce CPU work that starves the loop.
            await asyncio.sleep(0.01)
    assert "event_loop_lag lag_ms=" in caplog.text
    assert not any(t.get_name() == "omarkets-availability-monitor"
                   for t in asyncio.all_tasks())


@pytest.mark.asyncio
async def test_stall_stack_sample_contains_frames_but_not_values(caplog):
    secret_marker = "sensitive-payload-not-for-logs"
    with caplog.at_level(logging.WARNING):
        async with monitor_event_loop(
            interval=0.005,
            threshold=0.01,
            stack_threshold=0.02,
            sample_interval=0.005,
        ):
            await asyncio.sleep(0)
            end = time.perf_counter() + 0.08
            while time.perf_counter() < end:
                _ = secret_marker
            await asyncio.sleep(0.01)
    assert "event_loop_stack stalled_ms=" in caplog.text
    assert "test_stall_stack_sample_contains_frames_but_not_values" in caplog.text
    assert secret_marker not in caplog.text
