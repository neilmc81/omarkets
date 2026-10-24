from datetime import UTC, datetime

import pytest

from core.analysis.economics import month_before, numeric, transform_history, transform_observation
from core.domain.economics import MacroInput
from core.domain.macro_registry import MACRO_DEFINITIONS

NOW = datetime(2026, 9, 17, tzinfo=UTC)


def derive(id, values):
    definition = next(d for d in MACRO_DEFINITIONS if d.id == id)
    inputs = [
        MacroInput(
            source_series_id=definition.source_series_id,
            provider=definition.provider,
            period=p,
            value=v,
            unit="index points",
            received_at=NOW,
        )
        for p, v in values
    ]
    return transform_observation(definition, inputs, NOW, "https://example.test")


@pytest.mark.parametrize(
    ("id", "values", "expected"),
    [
        ("CPI_MOM", [("2026-01", 110), ("2025-12", 100)], 10),
        ("CPI_YOY", [("2025-08", 100), ("2026-08", 103)], 3),
        ("PAYROLLS", [("2026-07", 158913), ("2026-08", 159075)], 162),
        ("GDP", [("2026-Q2", 3.1)], 3.1),
        ("INITIAL_CLAIMS", [("2026-09-12", 200000)], 200000),
        ("CPI_MOM", [("2026-07", None), ("2026-08", 110)], None),
        ("CPI_MOM", [("2026-07", 0), ("2026-08", 110)], None),
        ("CPI_MOM", [("2026-07", 100), ("2026-08", None)], None),
    ],
)
def test_transform(id, values, expected):
    result = derive(id, values)
    assert result.value == expected
    assert result.released_at is None
    assert result.metadata.inputs[0].period == max(p for p, _ in values)


def test_missing_denominator_row_emits_no_observation():
    assert derive("CPI_MOM", [("2026-06", 100), ("2026-08", 110)]) is None


def test_empty_duplicates_and_month_boundaries():
    assert derive("CPI_MOM", []) is None
    assert month_before("2026-01", 1) == "2025-12"
    assert month_before("2024-02", 12) == "2023-02"
    with pytest.raises(ValueError):
        derive("CPI_MOM", [("2026-01", 100), ("2026-01", 101)])


@pytest.mark.parametrize("value", [None, "-", ".", ""])
def test_missing(value):
    assert numeric(value) is None


@pytest.mark.parametrize("value", [True, "nan", "inf", {}, "garbage"])
def test_invalid(value):
    with pytest.raises(ValueError):
        numeric(value)


def test_zero_is_not_missing():
    assert numeric("0") == 0


@pytest.mark.parametrize(
    "series_id,prior",
    [
        ("CPI_MOM", "2025-12"),
        ("CPI_YOY", "2025-01"),
        ("PAYROLLS", "2025-12"),
    ],
)
def test_rolling_window_preserves_history_across_reopen(tmp_path, series_id, prior):
    from core.providers.economic_base import EconomicBatch
    from core.storage.economics import SQLiteEconomicRepository

    definition = next(item for item in MACRO_DEFINITIONS if item.id == series_id)

    def history(values):
        inputs = [
            MacroInput(
                provider=definition.provider,
                source_series_id=definition.source_series_id,
                period=period,
                value=value,
                unit="index points",
                received_at=NOW,
            )
            for period, value in values
        ]
        return transform_history(definition, inputs, NOW, "https://example.test")

    repo = SQLiteEconomicRepository(tmp_path / "rolling.sqlite3", "live")
    repo.initialize()
    try:
        first = history([(prior, 100), ("2026-01", 110)])
        assert [item.period for item in first] == ["2026-01"]
        repo.store_batch(
            EconomicBatch(
                provider=definition.provider,
                checked_at=NOW,
                status="healthy",
                observations=first,
            )
        )
        original = repo.observations(all_versions=True)
        repo.close()
        repo.initialize()
        truncated = history([("2026-01", 110), ("2026-02", 121)])
        assert all(item.period != "2026-01" for item in truncated)
        repo.store_batch(
            EconomicBatch(
                provider=definition.provider,
                checked_at=NOW,
                status="healthy",
                observations=truncated,
            )
        )
        assert [
            item for item in repo.observations(all_versions=True) if item.period == "2026-01"
        ] == original
        genuine_null = history([(prior, None), ("2026-01", 110)])
        assert len(genuine_null) == 1 and genuine_null[0].value is None
        repo.store_batch(
            EconomicBatch(
                provider=definition.provider,
                checked_at=NOW,
                status="degraded",
                observations=genuine_null,
            )
        )
        repo.close()
        repo.initialize()
        versions = [
            item for item in repo.observations(all_versions=True) if item.period == "2026-01"
        ]
        assert [item.revision for item in versions] == [1, 2]
        assert versions[0].value is not None and versions[1].value is None
    finally:
        repo.close()


def test_identity_history_emits_single_and_source_null():
    definition = next(item for item in MACRO_DEFINITIONS if item.id == "GDP")
    inputs = [
        MacroInput(
            provider=definition.provider,
            source_series_id=definition.source_series_id,
            period="2026-Q1",
            value=None,
            unit=definition.unit,
            received_at=NOW,
        )
    ]
    result = transform_history(definition, inputs, NOW, "https://example.test")
    assert len(result) == 1
    assert result[0].period == "2026-Q1" and result[0].value is None


def test_registry_unique_and_adjustments():
    registry = {d.id: d for d in MACRO_DEFINITIONS}
    assert len(registry) == len(MACRO_DEFINITIONS)
    assert registry["CPI_YOY"].source_series_id.startswith("CUUR")
    assert registry["CORE_CPI_YOY"].source_series_id.startswith("CUUR")
    assert registry["CPI_MOM"].source_series_id.startswith("CUSR")
    assert registry["GDP"].transform == "identity"
    assert registry["ISM_MFG"].provider == "ism_newswire"
    assert registry["ISM_SERVICES"].provider == "ism_newswire"
