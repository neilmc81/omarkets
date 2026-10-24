"""Storage abstraction.

The repository interface keeps SQL out of the rest of the core. Callers depend
on :class:`ObservationRepository`; the DuckDB implementation lives in
:mod:`core.storage.duckdb_repo`.

Phase 1 uses a single writer (the market state service) on the event-loop
thread. DuckDB connections are not safe to share across threads, so the API
layer must never open its own connection.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from core.domain.models import MarketObservation


class StorageError(RuntimeError):
    """Raised when the storage backend fails."""


@dataclass(frozen=True)
class OhlcBucket:
    """Open/high/low/close of one symbol's observations in one time bucket.

    Built from stored observations only: a bucket with no observation is never
    returned. ``open`` and ``close`` are the earliest and latest by exchange
    time; when one exchange time was stored more than once (a revision), the
    latest receipt is used. Only quotes received within an hour after their
    exchange time (and not more than a minute before it) are used: anything
    else cannot be placed in time (a re-sent settle, a date-only quote).
    """

    start: datetime
    open: float
    high: float
    low: float
    close: float
    count: int
    first_at: datetime
    last_at: datetime
    #: Distinct sources in the bucket, sorted.
    sources: tuple[str, ...]
    #: Contract codes of the bucket's first and last quote, sorted (two after a roll).
    contracts: tuple[str, ...]
    #: True when the bucket's first or last quote came from a delayed feed.
    delayed: bool


class ObservationRepository(ABC):
    """Durable storage for market observations.

    All timestamps accepted and returned are timezone-aware UTC.
    """

    @abstractmethod
    def initialize(self) -> None:
        """Create the schema if needed. Idempotent."""

    @abstractmethod
    def store(self, observations: Iterable[MarketObservation]) -> int:
        """Persist observations. Returns the number written."""

    @abstractmethod
    def latest_per_instrument(self) -> dict[str, MarketObservation]:
        """Most recent observation per instrument, keyed by canonical symbol.

        "Most recent" is by event time (``timestamp``), tie-broken by
        ``received_at``, so out-of-order arrivals do not corrupt the result.
        """

    @abstractmethod
    def history(
        self,
        symbol: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
    ) -> Sequence[MarketObservation]:
        """Observations for one instrument, oldest first."""

    def ohlc(
        self,
        symbol: str,
        *,
        start: datetime,
        end: datetime,
        bucket_seconds: int,
        within: Sequence[tuple[datetime, datetime]] = (),
    ) -> Sequence[OhlcBucket]:
        """OHLC buckets for ``[start, end)``, aligned to multiples of
        ``bucket_seconds`` since the Unix epoch, oldest first.

        ``within`` optionally keeps only observations inside these half-open
        sub-windows (e.g. trading sessions), so a bucket never mixes in quotes
        from outside them."""
        raise StorageError("this storage backend cannot build OHLC buckets")

    @abstractmethod
    def count(self) -> int:
        """Total number of stored observations."""

    def compact(self, *, now: datetime) -> int:
        """Thin old observations; returns rows removed. Optional for a backend."""
        return 0

    @abstractmethod
    def health(self) -> bool:
        """Return True if the backend is reachable and the schema exists."""

    @abstractmethod
    def close(self) -> None:
        """Release the backend connection."""


__all__ = ["OhlcBucket", "ObservationRepository", "StorageError"]
