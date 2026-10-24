"""Official PPI current vintage: factory -> storage -> calendar."""

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from core.domain.economics import MacroRegistry
from core.domain.macro_registry import MACRO_DEFINITIONS
from core.providers.economic_base import EconomicBatch
from core.providers.economic_factory import build_economic_providers
from core.services.economics import EconomicService
from core.storage.economics import SQLiteEconomicRepository
from tests.services.test_calendar_current_actuals import event

SLOT = datetime(2026, 10, 15, 12, 30, tzinfo=UTC)
NOW = SLOT + timedelta(minutes=5)
FIXTURE = Path(__file__).parents[1] / "fixtures/economics/bls-ppi-2026-10-15.json"
METRICS = [
    ("PPI", "PPI m/m", "BLS_PPI_MOM", 1.3),
    ("PPI", "PPI y/y", "BLS_PPI_YOY", 6.3),
    ("CORE_PPI", "Core PPI m/m", "BLS_CORE_PPI_MOM", 0.7),
    ("CORE_PPI", "Core PPI y/y", "BLS_CORE_PPI_YOY", 5.0),
]


async def batch(payload=None, now=NOW):
    payload = json.loads(FIXTURE.read_text()) if payload is None else payload
    providers = build_economic_providers("live", clock=lambda: now)
    provider = next(p for p in providers if p.name == "bls")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        provider.client = client
        return await provider.fetch()


def core_for(result, *events, now=NOW):
    repository = SQLiteEconomicRepository(":memory:", "live")
    repository.initialize()
    core = EconomicService(
        repository=repository,
        mode="live",
        clock=lambda: now,
        registry=MacroRegistry(definitions=MACRO_DEFINITIONS),
    )
    core.accept_batch(result)
    core.accept_batch(
        EconomicBatch(provider="consensus", checked_at=now, events=list(events), status="healthy")
    )
    return core, repository


@pytest.mark.parametrize("code,title,series,rounded", METRICS)
async def test_factory_ppi_value_and_provenance(code, title, series, rounded):
    source = event(code, title, received_at=SLOT - timedelta(days=1), previous=0.4)
    result = await batch()
    core, repo = core_for(result, source)
    try:
        resolved = next(e for e in core.calendar_events() if e.id == source.id)
        assert round(resolved.actual, 1) == rounded
        assert resolved.actual_basis == "current_vintage"
        assert resolved.released_at is None
        assert resolved.previous == 0.4 and resolved.consensus == 0.3
        assert resolved.period == "2026-09" and resolved.unit == "percent"
        assert resolved.source_url == "https://api.bls.gov/publicAPI/v2/timeseries/data/"
        assert resolved.received_at == NOW
        observation = next(
            o for o in core.latest_observations() if o.id == series and o.period == "2026-09"
        )
        assert observation.metadata.inputs[0].source_revision
        denominator = "2025-09" if series.endswith("YOY") else "2026-08"
        assert [i.period for i in observation.metadata.inputs] == ["2026-09", denominator]
        assert all(i.released_at is None for i in observation.metadata.inputs)
    finally:
        repo.close()


@pytest.mark.parametrize(
    "change",
    [
        {"period": "2026-08"},
        {"unit": "index points"},
        {"received_at": SLOT - timedelta(seconds=1)},
        {"received_at": NOW + timedelta(seconds=1)},
        {"released_at": NOW + timedelta(seconds=1)},
        {"source_series_id": "WPSFD49116"},
        {"metadata": {"source_url": "https://example.com"}},
    ],
)
async def test_ppi_wrong_period_metric_basis_and_future_receipts_fail_closed(change):
    result = await batch()
    selected = next(
        o for o in result.observations if o.id == "BLS_PPI_MOM" and o.period == "2026-09"
    )
    changed = type(selected).model_validate(selected.model_dump() | change)
    result = result.model_copy(update={"observations": [changed]})
    item = event("PPI", "PPI m/m", received_at=SLOT - timedelta(days=1))
    core, repo = core_for(result, item)
    try:
        assert core.calendar_event_state(item).actual is None
    finally:
        repo.close()


async def test_bls_prior_revisions_stay_separate_and_preserve_input_history():
    original = await batch()
    payload = json.loads(FIXTURE.read_text())
    # Revise the headline SA prior level: current m/m changes too.
    payload["Results"]["series"][0]["data"][1]["value"] = "157.000"
    revised = await batch(payload, now=NOW + timedelta(seconds=1))
    item = event("PPI", "PPI m/m", previous=0.4, received_at=SLOT - timedelta(days=1))
    core, repo = core_for(original, item, now=NOW + timedelta(seconds=1))
    try:
        first = core.calendar_event_state(item)
        assert round(first.revised_previous, 1) == 0.5 and first.previous == 0.4
        core.accept_batch(revised)
        second = core.calendar_event_state(item)
        assert second.actual != first.actual
        assert second.previous == first.previous == 0.4
        assert second.revised_previous != first.revised_previous
        assert (
            next(
                o for o in original.observations if o.id == "BLS_PPI_MOM" and o.period == "2026-09"
            )
            .metadata.inputs[1]
            .value
            == 157.662
        )
    finally:
        repo.close()


async def test_missing_denominator_and_duplicates_never_invent_ppi_change():
    for mutate in ("missing", "duplicate"):
        payload = json.loads(FIXTURE.read_text())
        rows = payload["Results"]["series"][0]["data"]
        if mutate == "missing":
            rows[:] = [r for r in rows if (r["year"], r["period"]) != ("2026", "M08")]
        else:
            rows.append(copy.deepcopy(rows[0]))
        result = await batch(payload)
        assert not any(o.id == "BLS_PPI_MOM" and o.period == "2026-09" for o in result.observations)


def test_ppi_schedule_accelerates_bls_polling_and_keeps_fred_compatibility():
    core = EconomicService(mode="live", clock=lambda: SLOT)
    core._events = [event("CORE_PPI", "Core PPI m/m", received_at=SLOT)]
    assert core._next_interval("bls", 86400) == 900
    fred = {d.id: d.source_series_id for d in MACRO_DEFINITIONS if d.provider == "fred"}
    assert fred["PPI"] == "PPIFIS" and fred["CORE_PPI"] == "PPIFES"


async def test_ppi_monthly_annual_and_core_never_alias():
    rows = [
        event(code, title, received_at=SLOT - timedelta(days=1)) for code, title, _, _ in METRICS
    ]
    core, repo = core_for(await batch(), *rows)
    try:
        resolved = [core.calendar_event_state(row) for row in rows]
        assert [round(row.actual, 1) for row in resolved] == [m[3] for m in METRICS]
        # The separate ex-food/energy/trade aggregate cannot consume core data.
        unknown = event(
            "CORE_PPI", "Core PPI excluding trade m/m", received_at=SLOT - timedelta(days=1)
        )
        assert core.calendar_event_state(unknown).actual is None
    finally:
        repo.close()


async def test_annual_requires_exact_prior_year_month_and_never_uses_sa_monthly():
    payload = json.loads(FIXTURE.read_text())
    for entry in payload["Results"]["series"]:
        if entry["seriesID"] == "WPUFD4":
            entry["data"] = [
                row for row in entry["data"] if (row["year"], row["period"]) != ("2025", "M09")
            ]
    item = event("PPI", "PPI y/y", received_at=SLOT - timedelta(days=1))
    core, repo = core_for(await batch(payload), item)
    try:
        assert core.calendar_event_state(item).actual is None
    finally:
        repo.close()


async def test_prior_only_revision_advances_received_time():
    later = NOW + timedelta(seconds=1)
    payload = json.loads(FIXTURE.read_text())
    for entry in payload["Results"]["series"]:
        if entry["seriesID"] == "WPSFD4":
            for row in entry["data"]:
                if (row["year"], row["period"]) == ("2026", "M07"):
                    row["value"] = "156.000"
    item = event("PPI", "PPI m/m", previous=0.4, received_at=SLOT - timedelta(days=1))
    core, repo = core_for(await batch(), item)
    try:
        first = core.calendar_event_state(item)
        core.accept_batch(await batch(payload, now=later))
        # A future-received prior cannot enter the earlier evidence cutoff.
        frozen = core.calendar_event_state(item)
        assert frozen.revised_previous is None
        core.clock = lambda: later
        second = core.calendar_event_state(item)
        assert second.actual == first.actual
        assert second.previous == 0.4
        assert second.revised_previous == pytest.approx(100 * (157.662 / 156.000 - 1))
        assert second.received_at == later
        latest_actual = next(
            o for o in core.latest_observations() if o.id == "BLS_PPI_MOM" and o.period == "2026-09"
        )
        latest_prior = next(
            o for o in core._observations if o.id == "BLS_PPI_MOM" and o.period == "2026-08"
        )
        assert latest_actual.received_at == NOW
        assert latest_prior.received_at == later and latest_prior.revision == 2
        assert latest_prior.metadata.inputs[1].value == 156.000
        assert any(NOW.isoformat() in note and later.isoformat() in note for note in second.notes)
    finally:
        repo.close()
