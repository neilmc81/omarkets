from __future__ import annotations

from abc import ABC, abstractmethod

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
    def __init__(self, provider: str, detail: str) -> None:
        super().__init__(f"{provider}: {detail}")
        self.provider = provider
        self.detail = detail


__all__ = ["EconomicBatch", "EconomicProvider", "EconomicProviderError"]
