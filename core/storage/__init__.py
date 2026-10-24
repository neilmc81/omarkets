"""Storage for market observations.

Storage is the durable source of truth. The in-memory market state maintained
by :mod:`core.services.market_state` is a cache seeded from storage at startup.
"""

from core.storage.base import ObservationRepository, StorageError
from core.storage.duckdb_repo import DuckDBObservationRepository
from core.storage.events import EventRepositoryError, SQLiteEventRepository

__all__ = [
    "DuckDBObservationRepository",
    "EventRepositoryError",
    "ObservationRepository",
    "SQLiteEventRepository",
    "StorageError",
]
