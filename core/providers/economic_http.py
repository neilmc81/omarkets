from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from core.providers.economic_base import EconomicBatch, EconomicProvider, EconomicProviderError

Clock = Callable[[], datetime]
_sensitive_http = ContextVar("economic_sensitive_http", default=False)


@contextmanager
def sensitive_http_logs():
    factory = logging.getLogRecordFactory()
    if not getattr(factory, "_economic_redaction", False):

        def safe_record(*args, **kwargs):
            record = factory(*args, **kwargs)
            if _sensitive_http.get() and (
                record.name == "httpx"
                or record.name.startswith("httpx.")
                or record.name == "httpcore"
                or record.name.startswith("httpcore.")
            ):
                record.msg = "Sensitive economic HTTP operation; log details suppressed"
                record.args = ()
                record.exc_info = None
                record.exc_text = None
                record.stack_info = None
            return record

        safe_record._economic_redaction = True
        logging.setLogRecordFactory(safe_record)
    token = _sensitive_http.set(True)
    try:
        yield
    finally:
        _sensitive_http.reset(token)


def utc_now() -> datetime:
    return datetime.now(UTC)


class HttpEconomicProvider(EconomicProvider):
    provider_name = ""
    max_bytes = 2_000_000

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        clock: Clock = utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.client = client
        self.clock = clock
        self.sleep = sleep
        self._owned = client is None
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return self.provider_name

    async def aclose(self) -> None:
        if self._owned and self.client is not None:
            await self.client.aclose()
            self.client = None

    async def fetch(self) -> EconomicBatch:
        async with self._lock:
            now = self.clock()
            if now.tzinfo is None or now.utcoffset() is None:
                raise ValueError("clock must be timezone aware")
            try:
                batch = await self.collect(now)
            except Exception:
                batch = EconomicBatch(
                    provider=self.name,
                    checked_at=now,
                    status="unavailable",
                    issues=["Provider request or payload validation failed; details suppressed."],
                )
            return batch

    async def collect(self, now: datetime) -> EconomicBatch:
        raise NotImplementedError

    async def request(self, method: str, url: str, **kwargs) -> bytes:
        if self.client is None:
            self.client = httpx.AsyncClient(
                timeout=15,
                follow_redirects=False,
                limits=httpx.Limits(max_connections=2),
            )
        for attempt in range(2):
            delay = 1.0
            try:
                async with asyncio.timeout(20):
                    async with self.client.stream(
                        method, url, timeout=15, follow_redirects=False, **kwargs
                    ) as response:
                        if response.status_code in (429, 500, 502, 503, 504):
                            raw = response.headers.get("Retry-After")
                            if raw:
                                try:
                                    delay = float(raw)
                                except ValueError:
                                    delay = (
                                        parsedate_to_datetime(raw) - self.clock()
                                    ).total_seconds()
                            if delay > 30:
                                raise EconomicProviderError(self.name, "retry deferred")
                            if attempt:
                                raise EconomicProviderError(self.name, "transient failure")
                        elif response.status_code != 200:
                            raise EconomicProviderError(self.name, "HTTP request rejected")
                        else:
                            length = response.headers.get("Content-Length")
                            if length and int(length) > self.max_bytes:
                                raise EconomicProviderError(self.name, "payload too large")
                            body = bytearray()
                            async for chunk in response.aiter_bytes():
                                body.extend(chunk)
                                if len(body) > self.max_bytes:
                                    raise EconomicProviderError(self.name, "payload too large")
                            return bytes(body)
            except (httpx.TransportError, TimeoutError):
                if attempt:
                    raise EconomicProviderError(self.name, "transport unavailable") from None
            await self.sleep(max(0, min(delay, 30)))
        raise EconomicProviderError(self.name, "request unavailable")

    async def json_request(self, method: str, url: str, **kwargs):
        return json.loads(await self.request(method, url, **kwargs))
