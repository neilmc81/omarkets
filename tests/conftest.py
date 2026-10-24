"""Shared pytest fixtures.

All fixtures are deterministic and use isolated temporary storage. No test
reaches the network or the wall clock implicitly.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.storage.duckdb_repo import DuckDBObservationRepository

#: A fixed instant used by tests that need a stable clock.
FROZEN: datetime = datetime(2026, 1, 2, 15, 30, tzinfo=UTC)


@pytest.fixture
def frozen_now() -> datetime:
    return FROZEN


@pytest.fixture
def repo(tmp_path):
    """An isolated, initialized DuckDB repository in a temp directory."""
    repository = DuckDBObservationRepository(tmp_path / "test.duckdb")
    repository.initialize()
    yield repository
    repository.close()


@pytest.fixture
def memory_repo():
    """An isolated in-memory DuckDB repository."""
    repository = DuckDBObservationRepository(":memory:")
    repository.initialize()
    yield repository
    repository.close()
