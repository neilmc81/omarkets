from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from core.domain.economics import (
    EconomicEvent,
    EconomicsMode,
    Importance,
    MacroDefinition,
    MacroObservation,
    MacroRegistry,
)

OBSERVED_RELEASE_IMPORTANCE: dict[str, Importance] = {
    "CPI_MOM": Importance.CRITICAL,
    "CPI_YOY": Importance.CRITICAL,
    "CORE_CPI_MOM": Importance.CRITICAL,
    "CORE_CPI_YOY": Importance.CRITICAL,
    "PAYROLLS": Importance.CRITICAL,
    "PCE_YOY": Importance.CRITICAL,
    "CORE_PCE_YOY": Importance.CRITICAL,
    "UNEMPLOYMENT": Importance.HIGH,
    "AHE_MOM": Importance.HIGH,
    "AHE_YOY": Importance.HIGH,
    "PPI": Importance.HIGH,
    "CORE_PPI": Importance.HIGH,
    "GDP": Importance.HIGH,
    "RETAIL_SALES": Importance.HIGH,
    "INDUSTRIAL_PRODUCTION": Importance.HIGH,
    "JOLTS": Importance.HIGH,
    "INITIAL_CLAIMS": Importance.HIGH,
    "CONTINUING_CLAIMS": Importance.HIGH,
    "ISM_MFG": Importance.HIGH,
    "ISM_SERVICES": Importance.HIGH,
    "FED_TARGET_LOWER": Importance.HIGH,
    "FED_TARGET_UPPER": Importance.HIGH,
    "EFFR": Importance.MEDIUM,
    "SOFR": Importance.MEDIUM,
}

OBSERVED_PREFIX = "observed:"
RELEASE_NOTE = "Observed release derived from stored source observations."
RECEIVED_NOTE = (
    "Omarkets first received this value at the time shown; "
    "not the official source publication timestamp."
)
PUBLISHED_NOTE = "Publication timestamp supplied by the source."

NON_RELEASE_SERIES = frozenset({"EFFR", "SOFR", "FED_TARGET_LOWER", "FED_TARGET_UPPER"})


def importance_for(series_id: str) -> Importance:
    return OBSERVED_RELEASE_IMPORTANCE.get(series_id, Importance.MEDIUM)


def release_id(series_id: str, period: str) -> str:
    return f"{OBSERVED_PREFIX}{series_id}:{period}"


def parse_release_id(event_id: str) -> tuple[str, str] | None:
    if not event_id.startswith(OBSERVED_PREFIX):
        return None
    series_id, _, period = event_id[len(OBSERVED_PREFIX) :].partition(":")
    if not series_id or not period:
        return None
    return series_id, period


def _grouped(
    versions: list[MacroObservation],
) -> dict[tuple[str, str, str], dict[str, list[MacroObservation]]]:
    grouped: dict[tuple[str, str, str], dict[str, list[MacroObservation]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for item in versions:
        grouped[(item.provider, item.id, item.source_series_id)][item.period].append(item)
    for periods in grouped.values():
        for items in periods.values():
            items.sort(key=lambda row: (row.revision, row.received_at))
    return grouped


def _matches(latest: MacroObservation, definition: MacroDefinition) -> bool:
    return (
        latest.id == definition.id
        and latest.provider == definition.provider
        and latest.source_series_id == definition.source_series_id
        and latest.unit == definition.unit
    )


def _event(
    definition: MacroDefinition,
    period: str,
    history: list[MacroObservation],
    prior: list[MacroObservation] | None,
    mode: EconomicsMode,
) -> EconomicEvent | None:
    latest = history[-1]
    if not _matches(latest, definition) or latest.value is None:
        return None
    previous = None
    revised_previous = None
    if prior:
        original = prior[0]
        current = prior[-1]
        previous = original.value
        if current.value is not None and current.value != original.value:
            revised_previous = current.value
    notes = [RELEASE_NOTE, PUBLISHED_NOTE if latest.released_at else RECEIVED_NOTE]
    observed_at = latest.released_at or latest.received_at
    return EconomicEvent(
        id=release_id(definition.id, period),
        event_code=definition.id,
        name=definition.name,
        category=definition.category,
        importance=importance_for(definition.id),
        scheduled_at=None,
        period=period,
        unit=definition.unit,
        frequency=definition.frequency,
        status="released",
        actual=latest.value,
        actual_basis="source_release" if latest.released_at else "current_vintage",
        consensus=None,
        previous=previous,
        revised_previous=revised_previous,
        provider=latest.provider,
        source_event_id=f"{definition.id}:{period}",
        source_url=latest.metadata.source_url,
        released_at=observed_at,
        received_at=latest.received_at,
        revision=latest.revision,
        notes=notes,
        mode=mode,
    )


def observed_release_events(
    versions: list[MacroObservation],
    registry: MacroRegistry,
    *,
    mode: EconomicsMode,
    now: datetime,
) -> list[EconomicEvent]:
    if mode == EconomicsMode.DISABLED:
        return []
    grouped = _grouped(versions)
    events: list[EconomicEvent] = []
    for definition in registry.definitions:
        if definition.id in NON_RELEASE_SERIES:
            continue
        periods = grouped.get((definition.provider, definition.id, definition.source_series_id))
        if not periods:
            continue
        ordered = sorted(periods)
        period = ordered[-1]
        index = len(ordered) - 1
        prior = periods[ordered[index - 1]] if index > 0 else None
        built = _event(definition, period, periods[period], prior, mode)
        if built is not None:
            events.append(built)
    return events


def release_history(
    versions: list[MacroObservation],
    definition: MacroDefinition,
    period: str,
    *,
    mode: EconomicsMode,
) -> list[EconomicEvent]:
    grouped = _grouped(versions)
    periods = grouped.get((definition.provider, definition.id, definition.source_series_id))
    if not periods or period not in periods:
        return []
    ordered = sorted(periods)
    prior = periods[ordered[ordered.index(period) - 1]] if ordered.index(period) > 0 else None
    results = []
    for index in range(len(periods[period])):
        built = _event(definition, period, periods[period][: index + 1], prior, mode)
        if built is not None:
            results.append(built.model_copy(update={"revision": periods[period][index].revision}))
    return results
