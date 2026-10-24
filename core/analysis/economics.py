from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from math import isfinite

from core.domain.economics import MacroDefinition, MacroInput, MacroObservation, MacroProvenance


def numeric(value: object) -> float | None:
    if value is None or value in ("-", ".", ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("invalid numeric value")
    try:
        result = float(Decimal(str(value)))
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise ValueError("invalid numeric value") from exc
    if not isfinite(result):
        raise ValueError("nonfinite numeric value")
    return result


def month_before(period: str, months: int) -> str:
    date.fromisoformat(period + "-01")
    index = int(period[:4]) * 12 + int(period[5:]) - 1 - months
    year, month = divmod(index, 12)
    return f"{year:04d}-{month + 1:02d}"


def transform_observation(
    definition: MacroDefinition,
    inputs: list[MacroInput],
    received_at: datetime,
    source_url: str,
    notes: list[str] | None = None,
    *,
    period: str | None = None,
) -> MacroObservation | None:
    by_period: dict[str, MacroInput] = {}
    for item in inputs:
        if (
            item.provider != definition.provider
            or item.source_series_id != definition.source_series_id
        ):
            raise ValueError("input provenance does not match definition")
        if item.period in by_period:
            raise ValueError("duplicate input period")
        by_period[item.period] = item
    if not by_period:
        return None
    period = period or max(by_period)
    if period not in by_period:
        return None
    current = by_period[period]
    used = [current]
    value = current.value
    transform = definition.transform or "identity"
    if transform != "identity":
        if transform not in ("mom", "yoy", "difference"):
            raise ValueError("unsupported transform")
        previous = by_period.get(month_before(period, 12 if transform == "yoy" else 1))
        if previous is None:
            return None
        value = None
        used.append(previous)
        if current.value is not None and previous.value is not None:
            a, b = Decimal(str(current.value)), Decimal(str(previous.value))
            if transform == "difference":
                value = float(a - b)
            elif b > 0:
                value = float(100 * (a / b - 1))
    output_notes = list(notes or [])
    if transform != "identity":
        output_notes.append("Derived from current-vintage levels; not an original-release actual.")
    if value is None:
        output_notes.append("Required value or exact reference-period denominator unavailable.")
    return MacroObservation(
        id=definition.id,
        period=period,
        value=value,
        unit=definition.unit,
        source_series_id=definition.source_series_id,
        provider=definition.provider,
        received_at=received_at,
        released_at=current.released_at,
        notes=output_notes,
        metadata=MacroProvenance(
            source_url=source_url,
            source_revision=current.source_revision,
            transform=transform,
            inputs=used,
        ),
    )


def transform_history(
    definition: MacroDefinition,
    inputs: list[MacroInput],
    received_at: datetime,
    source_url: str,
    notes: list[str] | None = None,
) -> list[MacroObservation]:
    results = []
    for period in sorted({item.period for item in inputs}):
        observation = transform_observation(
            definition,
            inputs,
            received_at,
            source_url,
            notes,
            period=period,
        )
        if observation is not None:
            results.append(observation)
    return results
