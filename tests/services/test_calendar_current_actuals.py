from datetime import UTC, datetime, timedelta

import pytest

from core.domain.economics import EconomicEvent, MacroObservation
from core.domain.macro_registry import MACRO_DEFINITIONS
from core.services.calendar_projection import project_calendar
from core.services.economics import EconomicService

NOW = datetime(2026, 10, 15, 16, tzinfo=UTC)
SLOT = datetime(2026, 10, 15, 12, 30, tzinfo=UTC)


def event(code="CPI", title="CPI m/m", **changes):
    return EconomicEvent.model_validate(
        dict(
            id=title,
            event_code=code,
            name=title,
            category="inflation",
            importance="high",
            scheduled_at=SLOT,
            provider="consensus",
            source_event_id=title,
            unit="percent",
            frequency="release",
            status="due",
            received_at=NOW,
            mode="live",
            consensus=0.3,
            previous=0.2,
            source_url="https://nfs.faireconomy.media/ff_calendar_thisweek.json",
        )
        | changes
    )


def service(series, item):
    definition = next(d for d in MACRO_DEFINITIONS if d.id == series)
    result = EconomicService(mode="live", clock=lambda: NOW)
    result._events = [
        item,
        event(
            "CPI",
            "Consumer Price Index",
            id="official",
            provider="fred_release_calendar",
            consensus=None,
            previous=None,
        ),
    ]
    result._observations = [
        MacroObservation(
            id=series,
            provider=definition.provider,
            source_series_id=definition.source_series_id,
            period="2026-09",
            value=0.4,
            unit=definition.unit,
            received_at=NOW,
            metadata={
                "source_url": (
                    "https://api.bls.gov/publicAPI/v2/timeseries/data/"
                    if definition.provider == "bls"
                    else f"https://fred.stlouisfed.org/series/{definition.source_series_id}"
                )
            },
        )
    ]
    return result


@pytest.mark.parametrize(
    ("code", "title", "series"),
    [
        ("CPI", "CPI m/m", "CPI_MOM"),
        ("CPI", "CPI y/y", "CPI_YOY"),
        ("CORE_CPI", "Core CPI m/m", "CORE_CPI_MOM"),
        ("CORE_CPI", "Core CPI y/y", "CORE_CPI_YOY"),
        ("PPI", "PPI m/m", "PPI"),
        ("CORE_PPI", "Core PPI m/m", "CORE_PPI"),
        ("RETAIL_SALES", "Retail Sales m/m", "RETAIL_SALES"),
        ("CORE_RETAIL_SALES", "Core Retail Sales m/m", "CORE_RETAIL_SALES"),
    ],
)
def test_current_vintage_exact_metric_retains_forecast_previous_and_identity(code, title, series):
    item = event(code, title)
    core = service(series, item)
    result = core.calendar_event_state(item)
    assert result.actual == 0.4 and result.actual_basis == "current_vintage"
    assert result.consensus == 0.3 and result.previous == 0.2
    assert result.raw_surprise == pytest.approx(0.1)
    assert result.id == item.id and result.source_event_id == item.source_event_id
    assert result.released_at is None  # Retrieval/schedule is not a publication instant.
    assert result.period == "2026-09"
    assert any(
        "original release value and publication instant are unverified" in n for n in result.notes
    )


def test_legacy_core_retail_code_is_corrected_without_rewriting_source():
    item = event("RETAIL_SALES", "Core Retail Sales m/m")
    result = service("CORE_RETAIL_SALES", item).calendar_event_state(item)
    assert result.event_code == "CORE_RETAIL_SALES" and result.actual == 0.4
    assert item.event_code == "RETAIL_SALES"


@pytest.mark.parametrize("value", [0.0, None])
def test_zero_is_a_measured_actual_and_missing_is_unavailable(value):
    item = event("PPI", "PPI m/m")
    core = service("PPI", item)
    core._observations[0] = core._observations[0].model_copy(update={"value": value})
    result = core.calendar_event_state(item)
    assert result.actual == value
    assert result.raw_surprise == (-0.3 if value == 0.0 else None)
    assert result.actual_basis == ("current_vintage" if value == 0.0 else None)
    assert result.consensus == 0.3 and result.previous == 0.2


def test_current_revision_conflicts_fail_closed_and_new_revision_keeps_provenance():
    item = event("PPI", "PPI m/m")
    core = service("PPI", item)
    original = core._observations[0]
    core._observations.append(original.model_copy(update={"value": 0.5}))
    assert core.calendar_event_state(item).actual is None
    core._observations[-1] = original.model_copy(
        update={
            "value": 0.5,
            "revision": 2,
            "metadata": original.metadata.model_copy(update={"source_revision": "revised-index"}),
        }
    )
    result = core.calendar_event_state(item)
    assert result.actual == 0.5 and result.actual_basis == "current_vintage"
    assert result.source_revision == "revised-index"
    assert result.previous == 0.2 and result.revised_previous is None
    assert result.released_at is None
    assert core._observations[0] == original and item.actual is None


@pytest.mark.parametrize(
    "change",
    [
        {"period": "2026-08"},
        {"received_at": SLOT - timedelta(seconds=1)},
        {"received_at": NOW + timedelta(seconds=1)},
        {"source_series_id": "wrong"},
        {"unit": "index points"},
        {"metadata": {"source_url": "https://example.com/series/PPIFIS"}},
    ],
)
def test_rejects_old_month_future_prepublication_wrong_metric_units_or_provenance(change):
    item = event("PPI", "PPI m/m")
    core = service("PPI", item)
    core._observations = [
        MacroObservation.model_validate(core._observations[0].model_dump() | change)
    ]
    assert core.calendar_event_state(item).actual is None


def test_no_headline_substitution_for_core_or_annual_metric():
    for item in [event("CORE_CPI", "Core CPI m/m"), event("CPI", "CPI y/y")]:
        assert service("CPI_MOM", item).calendar_event_state(item).actual is None
    for title in ["Core PPI excluding trade y/y", "PPI", "Core Retail Sales Control Group m/m"]:
        item = event("CORE_PPI" if "PPI" in title else "CORE_RETAIL_SALES", title)
        # Unknown exclusion/control-group titles must not consume the ex-auto series.
        assert EconomicService._calendar_series(item) is None


def test_cpi_requires_official_elapsed_slot_and_unique_metric():
    item = event()
    core = service("CPI_MOM", item)
    core._events = [item]
    assert core.calendar_event_state(item).actual is None
    core = service("CPI_MOM", item)
    core._events.append(item.model_copy(update={"id": "duplicate"}))
    assert core.calendar_event_state(item).actual is None
    item = event(scheduled_at=NOW + timedelta(days=1))
    assert service("CPI_MOM", item).calendar_event_state(item).actual is None


def test_unsupported_families_have_specific_coverage_explanations():
    core = EconomicService(mode="live", clock=lambda: NOW)
    for code, phrase in [
        ("GDP", "BEA publication notice"),
        ("NEW_HOME_SALES", "Census new-home-sales"),
        ("CORE_PCE", "core PCE"),
    ]:
        result = core.calendar_event_state(event(code, code))
        assert result.actual is None and any(phrase in note for note in result.notes)


def test_issuer_join_preserves_aliases_revision_history_and_original_schedule():
    from core.providers.economic_fed_g17 import parse_release
    from tests.providers.test_current_issuer_actuals import G17

    issuer = parse_release(G17, NOW)
    schedule = event(
        "INDUSTRIAL_PRODUCTION",
        "Industrial Production m/m",
        scheduled_at=issuer.released_at,
        consensus=0.2,
        previous=0.1,
    )
    result = project_calendar([issuer, schedule], now=NOW)
    assert len(result) == 1 and result[0].actual == 0.0
    assert result[0].actual_basis == "source_release" and result[0].raw_surprise == -0.2
    assert {e.id for e in result[0].occurrence_sources} == {issuer.id, schedule.id}
    assert result[0].revision == issuer.revision + schedule.revision
    assert project_calendar(result, now=NOW) == result
    duplicate = schedule.model_copy(update={"id": "another-slot"})
    assert len(project_calendar([issuer, schedule, duplicate], now=NOW)) == 3


def test_census_api_value_joins_only_the_slot_for_the_month_it_covers():
    """The Census API has no publication time: September retail sales join the
    October 16 8:30 ET slot; a stale August value never fills that slot."""
    from core.providers.economic_census_retail import parse_series
    from tests.providers.test_current_issuer_actuals import CENSUS, CENSUS_BODY, census_body

    now = datetime(2026, 10, 23, 2, tzinfo=UTC)
    slot = datetime(2026, 10, 16, 12, 30, tzinfo=UTC)
    schedule = event(
        "RETAIL_SALES", "Retail Sales m/m", scheduled_at=slot, consensus=0.5, previous=1.3,
        received_at=now,
    )
    september = parse_series(CENSUS_BODY, now)
    [joined] = project_calendar([september, schedule], now=now)
    assert joined.actual == 0.8 and joined.actual_basis == "source_release"
    assert joined.released_at is None
    august = parse_series(census_body(CENSUS), now)
    assert august.period == "2026-08"
    result = project_calendar([august, schedule], now=now)
    assert len(result) == 2 and all(e.actual in (None, 1.3) for e in result)
    assert next(e for e in result if e.provider == "consensus").actual is None
