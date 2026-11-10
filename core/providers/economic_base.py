from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.domain.economics import (
    EconomicEvent,
    EconomicProviderStatus,
    MacroObservation,
    UTCDatetime,
)


class EconomicBatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = Field(min_length=1)
    checked_at: UTCDatetime
    status: EconomicProviderStatus
    observations: list[MacroObservation] = Field(default_factory=list)
    events: list[EconomicEvent] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    checked_series_ids: list[str] | None = None

    @model_validator(mode="after")
    def _matching_provider(self) -> EconomicBatch:
        if any(item.provider != self.provider for item in (*self.observations, *self.events)):
            raise ValueError("batch payload provider must match batch provider")
        return self


class EconomicProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    async def fetch(self) -> EconomicBatch:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


class EconomicProviderError(RuntimeError):
    def __init__(self, provider: str, detail: str, status_code: int | None = None) -> None:
        super().__init__(f"{provider}: {detail}")
        self.provider = provider
        self.detail = detail
        self.status_code = status_code


class ReleasePending(ValueError):
    """A release dated a little after the local clock: not out yet, not a fault.

    Seen when an issuer posts its file a moment before the stated release time
    or the local clock lags. Providers report this as a healthy check with no
    new event, so the feed is not marked down at the bell.
    """


# A release time further ahead than this is not "just early"; it is a bad date
# and still fails the check.
PENDING_HORIZON = timedelta(days=1)


def require_published(published: datetime, now: datetime, what: str) -> None:
    """Raise ``ReleasePending`` for a near-future release, ``ValueError`` if far off."""
    if published <= now:
        return
    if published - now <= PENDING_HORIZON:
        raise ReleasePending(f"{what} is not released yet")
    raise ValueError(f"{what} publication time is too far in the future")


__all__ = [
    "PENDING_HORIZON",
    "EconomicBatch",
    "EconomicProvider",
    "EconomicProviderError",
    "ReleasePending",
    "require_published",
]
