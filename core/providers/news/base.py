"""News source adapter framework.

Every news/geopolitical source implements :class:`NewsSource` and returns typed
:class:`~core.domain.news.SourceItem` objects. Vendor payloads never cross this
boundary. Adding a source means writing one adapter and registering it; the
pipeline, storage, API and UI are untouched.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from core.domain.news import (
    SourceHealth,
    SourceItem,
    SourceMetadata,
    SourceStatus,
    SourceType,
)

Clock = Callable[[], datetime]

#: After HTTP 429 a source waits Retry-After, or this long when none is given.
RATE_LIMIT_BACKOFF_SECONDS = 900.0
MAX_BACKOFF_SECONDS = 6 * 3600.0


def utc_now() -> datetime:
    return datetime.now(UTC)


class NewsSourceError(RuntimeError):
    def __init__(self, source: str, detail: str) -> None:
        super().__init__(f"{source}: {detail}")
        self.source = source
        self.detail = detail


class NewsSource(ABC):
    """A pollable news/geopolitical source.

    Construction must not perform I/O. ``fetch`` is called by the service on a
    schedule and must return only validated items; malformed items are skipped and
    reported as issues, never raised.
    """

    source_type: SourceType = SourceType.NEWS

    def __init__(self, *, clock: Clock = utc_now, enabled: bool = True) -> None:
        self.clock = clock
        self.enabled = enabled
        self._last_attempt_at: datetime | None = None
        self._last_success_at: datetime | None = None
        self._last_error: str | None = None
        self._consecutive_failures = 0
        self._recent_event_count = 0
        self._detail: str | None = None

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable adapter identifier, e.g. ``fed_press``."""

    @property
    @abstractmethod
    def metadata(self) -> SourceMetadata:
        """Static, serializable description of the source."""

    @abstractmethod
    async def fetch(self) -> list[SourceItem]:
        """Fetch and validate currently available items."""

    async def aclose(self) -> None:
        return None

    def record_success(self, count: int, detail: str | None = None) -> None:
        self._last_attempt_at = self.clock()
        self._last_success_at = self._last_attempt_at
        self._consecutive_failures = 0
        self._recent_event_count = count
        self._last_error = None
        self._detail = detail

    def record_failure(self, error: str) -> None:
        self._last_attempt_at = self.clock()
        self._consecutive_failures += 1
        self._last_error = error
        self._detail = None

    def health(self) -> SourceHealth:
        if not self.enabled:
            status = SourceStatus.DISABLED
        elif self._last_error is not None:
            status = (
                SourceStatus.OFFLINE if self._consecutive_failures >= 3 else SourceStatus.DEGRADED
            )
        elif self._last_success_at is None:
            status = SourceStatus.DEGRADED
        else:
            status = SourceStatus.HEALTHY
        return SourceHealth(
            name=self.name,
            source_type=self.metadata.source_type,
            status=status,
            enabled=self.enabled,
            last_attempt_at=self._last_attempt_at,
            last_success_at=self._last_success_at,
            last_error=self._last_error,
            recent_event_count=self._recent_event_count,
            consecutive_failures=self._consecutive_failures,
            detail=self._detail,
        )


class HttpNewsSource(NewsSource):
    """Base for adapters that fetch over HTTP.

    Provides bounded reads, a single retry with backoff for transient failures,
    payload-size limits, and explicit failure isolation: any transport or
    validation error becomes a recorded failure, never an exception that can
    crash the scheduler.
    """

    max_bytes = 2_000_000
    request_timeout = 15.0

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Clock = utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        enabled: bool = True,
    ) -> None:
        super().__init__(clock=clock, enabled=enabled)
        self.client = client
        self.sleep = sleep
        self._owned = client is None
        #: Minimum wait before the next poll, set when the server asks to back off.
        self.backoff_seconds = 0.0

    async def aclose(self) -> None:
        if self._owned and self.client is not None:
            await self.client.aclose()
            self.client = None

    async def request(self, method: str, url: str, **kwargs) -> bytes:
        if self.client is None:
            self.client = httpx.AsyncClient(
                timeout=self.request_timeout,
                follow_redirects=True,
                limits=httpx.Limits(max_connections=2),
                # Some official feeds reject non-browser user agents. Identify the
                # client honestly while remaining compatible with those servers.
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (X11; Linux x86_64) Omarkets/0.1 (+local workstation)"
                    ),
                    "Accept": (
                        "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"
                    ),
                },
            )
        self.backoff_seconds = 0.0
        for attempt in range(2):
            delay = 1.0
            try:
                async with asyncio.timeout(self.request_timeout + 5):
                    async with self.client.stream(
                        method, url, timeout=self.request_timeout, **kwargs
                    ) as response:
                        status = response.status_code
                        retry_after = self._retry_after(response.headers.get("Retry-After"))
                        if status == 429:
                            # Rate limited: no immediate retry; skip polls until allowed.
                            wait = retry_after
                            if wait is None:
                                wait = RATE_LIMIT_BACKOFF_SECONDS
                            self.backoff_seconds = min(wait, MAX_BACKOFF_SECONDS)
                            raise NewsSourceError(self.name, "rate limited (HTTP 429)")
                        if status in (500, 502, 503, 504):
                            if retry_after is not None:
                                delay = retry_after
                            if delay > 30:  # too long to wait inside this poll
                                self.backoff_seconds = min(delay, MAX_BACKOFF_SECONDS)
                            if delay > 30 or attempt:
                                raise NewsSourceError(
                                    self.name, f"transient failure (HTTP {status})"
                                )
                        elif status != 200:
                            raise NewsSourceError(
                                self.name, f"HTTP request rejected (HTTP {status})"
                            )
                        else:
                            length = response.headers.get("Content-Length")
                            if length and int(length) > self.max_bytes:
                                raise NewsSourceError(self.name, "payload too large")
                            body = bytearray()
                            async for chunk in response.aiter_bytes():
                                body.extend(chunk)
                                if len(body) > self.max_bytes:
                                    raise NewsSourceError(self.name, "payload too large")
                            return bytes(body)
            except (httpx.TransportError, TimeoutError) as exc:
                if attempt:
                    raise NewsSourceError(self.name, "transport unavailable") from exc
            await self.sleep(max(0.0, min(delay, 30.0)))
        raise NewsSourceError(self.name, "request unavailable")

    def _retry_after(self, raw: str | None) -> float | None:
        """Seconds from a Retry-After header (delta or HTTP date); None if absent/invalid."""
        if not raw:
            return None
        try:
            seconds = float(raw)
        except ValueError:
            try:
                seconds = (parsedate_to_datetime(raw) - self.clock()).total_seconds()
            except (TypeError, ValueError):
                return None
        return max(0.0, seconds) if seconds == seconds else None


def content_hash(payload: object) -> str:
    """Stable content hash of a JSON-serializable payload."""
    try:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        encoded = repr(payload)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


__all__ = [
    "Clock",
    "HttpNewsSource",
    "NewsSource",
    "NewsSourceError",
    "content_hash",
    "utc_now",
]
