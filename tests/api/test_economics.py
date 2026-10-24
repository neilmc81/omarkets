from datetime import timedelta

import pytest
from starlette.testclient import TestClient

from core.api.app import create_app
from core.api.economics import (
    CalendarResponse,
    EventHistoryResponse,
    MacroHistoryResponse,
    MacroSeriesResponse,
    ProvidersResponse,
    calendar_display_groups,
)
from core.domain.economics import MacroState
from core.providers.fake import FakeMarketDataProvider
from core.services.economics import EconomicMessage, EconomicMessageType, EconomicService
from core.services.market_state import MarketStateService
from core.storage.duckdb_repo import DuckDBObservationRepository
from core.storage.economics import SQLiteEconomicRepository
from tests.services.test_economics import T0, FakeClock, batch, event, registry


@pytest.fixture
def client(tmp_path):
    economics = EconomicService(
        repository=SQLiteEconomicRepository(tmp_path / "economic.sqlite3", "live"),
        registry=registry(),
        mode="live",
        clock=FakeClock(),
    )
    market = MarketStateService(
        provider=FakeMarketDataProvider(update_interval=1000),
        repository=DuckDBObservationRepository(":memory:"),
    )
    with TestClient(create_app(service=market, economic_service=economics)) as client:
        client.portal.call(economics.accept_batch, batch())
        yield client, economics, market
    assert economics.subscriber_count == market.subscriber_count == 0


@pytest.mark.parametrize(
    "path,schema",
    [
        ("/macro/state", MacroState),
        ("/macro/series", MacroSeriesResponse),
        ("/macro/series/CPI_YOY", MacroHistoryResponse),
        ("/calendar", CalendarResponse),
        ("/calendar/today", CalendarResponse),
        ("/calendar/week", CalendarResponse),
        ("/calendar/upcoming", CalendarResponse),
        ("/calendar/cpi-release-2026-09", EventHistoryResponse),
        ("/providers", ProvidersResponse),
    ],
)
def test_response_contracts(client, path, schema):
    http, _, _ = client
    response = http.get("/api/v1" + path)
    assert response.status_code == 200
    schema.model_validate(response.json())


def test_calendar_display_groups_keep_source_records_and_prefer_forecast():
    scheduled = event(
        id="fred:cpi-slot",
        provider="fred_release_calendar",
        source_event_id="10:2026-09-17",
    )
    consensus = event(
        id="consensus:cpi-slot",
        provider="consensus",
        source_event_id="cpi",
        consensus=0.3,
    )
    unrelated = event(id="other:ppi", event_code="PPI", source_event_id="ppi")
    groups = calendar_display_groups([scheduled, consensus, unrelated])
    cpi = next(group for group in groups if group.event_code == "CPI")
    assert cpi.primary_event_id == consensus.id
    assert cpi.event_ids == sorted([scheduled.id, consensus.id])
    assert len(groups) == 2
    assert unrelated.id in next(group for group in groups if group.event_code == "PPI").event_ids


def test_calendar_api_exposes_source_group_without_merging_records(client):
    from core.providers.economic_base import EconomicBatch

    http, economics, _ = client
    fred = event(
        id="fred:cpi-slot",
        provider="fred_release_calendar",
        source_event_id="10:2026-09-17",
    )
    http.portal.call(
        economics.accept_batch,
        EconomicBatch(
            provider="fred_release_calendar",
            checked_at=T0,
            status="healthy",
            events=[fred],
        ),
    )
    body = http.get("/api/v1/calendar").json()
    group = next(item for item in body["display_groups"] if fred.id in item["event_ids"])
    assert group["event_ids"] == sorted([fred.id, "cpi-release-2026-09"])
    assert {item["id"] for item in body["events"]} >= set(group["event_ids"])
    assert "source-published schedule" in " ".join(body["coverage"])


def test_calendar_groups_ism_issuer_notice_with_complete_scheduled_record():
    scheduled = event(
        id="consensus:ism-prices", event_code="ISM_MFG_PRICES",
        provider="consensus", period="2026-08", unit="index points",
        actual=77.9, consensus=78.0, previous=78.2, frequency="release",
    )
    notice = event(
        id="issuer:ism-prices", event_code="ISM_MFG_PRICES",
        provider="ism_newswire", period="2026-08", unit="index points",
        scheduled_at=None, released_at=T0, actual=77.9,
        actual_basis="source_release",
    )
    groups = calendar_display_groups([notice, scheduled])
    assert len(groups) == 1
    assert groups[0].primary_event_id == scheduled.id
    assert groups[0].event_ids == sorted([notice.id, scheduled.id])
    assert notice.consensus is None
    assert notice.scheduled_at is None


@pytest.mark.parametrize("updates", [
    {"period": "2026-07"}, {"period": None},
    {"released_at": T0 + timedelta(seconds=1)},
    {"unit": "percent"}, {"country": "CA"},
    {"event_code": "ISM_MFG"}, {"provider": "unverified"},
    {"frequency": "quarterly"}, {"actual": 77.8},
])
def test_calendar_does_not_group_ambiguous_ism_notices(updates):
    scheduled = event(
        id="consensus:ism-prices", event_code="ISM_MFG_PRICES",
        provider="consensus", period="2026-08", unit="index points",
        actual=77.9,
    )
    notice = event(
        id="issuer:ism-prices", event_code="ISM_MFG_PRICES",
        provider="ism_newswire", period="2026-08", unit="index points",
        scheduled_at=None, released_at=T0, actual=77.9,
        actual_basis="source_release",
    ).model_copy(update=updates)
    assert all(notice.id not in group.event_ids
               for group in calendar_display_groups([scheduled, notice]))


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=501",
        "days=366",
        "importance=bogus",
        "start=2026-09-17T12:00:00",
        "start=2026-09-18T00:00:00Z&end=2026-09-17T00:00:00Z",
    ],
)
def test_calendar_validation(client, query):
    assert client[0].get("/api/v1/calendar?" + query).status_code == 422


def test_filters_history_and_failures(client):
    http, economics, _ = client
    assert http.get("/api/v1/calendar?status=due").json()["events"]
    assert not http.get("/api/v1/calendar?importance=low").json()["events"]
    assert http.get("/api/v1/macro/series/unknown").status_code == 404
    assert http.get("/api/v1/calendar/unknown").status_code == 404
    for path in ("/macro/series/CPI_YOY", "/calendar/cpi-release-2026-09"):
        assert http.get("/api/v1" + path + "?limit=501").status_code == 422
    http.portal.call(economics.accept_batch, batch(events=[event(actual=0.4)]))
    body = http.get("/api/v1/calendar/cpi-release-2026-09?limit=1").json()
    assert len(body["versions"]) == 1
    assert body["event"]["revision"] == 2
    assert http.get("/health").json()["economic_providers"][0]["status"] == "healthy"
    http.portal.call(economics.repository.close)
    assert http.get("/api/v1/macro/series/CPI_YOY").status_code == 503
    assert http.get("/api/v1/calendar/cpi-release-2026-09").status_code == 503


def test_observed_release_is_discoverable_and_has_history(client):
    http, _, _ = client
    listing = http.get("/api/v1/calendar?status=released").json()["events"]
    observed = [item for item in listing if item["id"].startswith("observed:")]
    assert len(observed) == 1
    item = observed[0]
    assert item["actual"] == 3.2
    assert item["consensus"] is None
    assert item["scheduled_at"] is None
    assert item["released_at"] is not None
    detail = http.get("/api/v1/calendar/" + item["id"]).json()
    assert detail["event"]["id"] == item["id"]
    assert [version["actual"] for version in detail["versions"]] == [3.2]
    assert detail["history_scope"] == "observed_since_install"


def test_observed_release_reports_previous_and_revised(client):
    from tests.services.test_economics import observation

    http, economics, _ = client
    http.portal.call(
        economics.accept_batch,
        batch(
            observations=[
                observation(period="2026-07", value=3.0),
                observation(period="2026-08", value=3.2),
                observation(period="2026-07", value=3.1, revision=2),
            ]
        ),
    )
    item = next(
        event
        for event in http.get("/api/v1/calendar?status=released").json()["events"]
        if event["id"] == "observed:CPI_YOY:2026-08"
    )
    assert item["actual"] == 3.2
    assert item["previous"] == 3.0
    assert item["revised_previous"] == 3.1
    assert item["raw_surprise"] is None


def test_websocket_all_types_and_cleanup(client):
    http, economics, market = client
    with http.websocket_connect("/ws/market?topics=macro") as ws:
        assert ws.receive_json()["type"] == "state"
        for kind in EconomicMessageType:
            http.portal.call(economics._publish, kind, ["one", "two"])
            message = EconomicMessage.model_validate(ws.receive_json())
            assert message.type == kind
            assert message.data.ids == ["one", "two"]
            assert message.data.occurred_at == T0
    assert economics.subscriber_count == market.subscriber_count == 0


@pytest.mark.parametrize("optional_provider", ["fred", "consensus"])
def test_optional_macro_provider_does_not_make_healthy_calendar_stale(client, optional_provider):
    from core.providers.economic_base import EconomicBatch

    http, economics, _ = client
    http.portal.call(
        economics.accept_batch,
        EconomicBatch(provider=optional_provider, checked_at=T0, status="not_configured"),
    )
    response = http.get("/api/v1/calendar/today")
    assert response.status_code == 200
    body = response.json()
    assert {item["id"] for item in body["events"]} == {
        "cpi-release-2026-09",
        "observed:CPI_YOY:2026-08",
    }
    health = {item["name"]: item["status"] for item in body["providers"]}
    assert health == {"bls": "healthy", optional_provider: "not_configured"}
    assert body["stale"] is False


def test_default_socket_does_not_subscribe_to_macro(client):
    http, economics, _ = client
    with http.websocket_connect("/ws/market") as ws:
        assert ws.receive_json()["type"] == "state"
        assert economics.subscriber_count == 0


def test_none_injection_is_disabled_and_never_builds_providers(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network providers must not be constructed")

    monkeypatch.setattr("core.providers.economic_factory.build_economic_providers", forbidden)
    market = MarketStateService(
        provider=FakeMarketDataProvider(update_interval=1000),
        repository=DuckDBObservationRepository(":memory:"),
    )
    with TestClient(create_app(service=market)) as http:
        assert http.get("/api/v1/macro/state").json()["mode"] == "disabled"
        assert http.get("/health").json()["economic_providers"] == []
        with http.websocket_connect("/ws/market?topics=macro") as ws:
            assert ws.receive_json()["type"] == "error"


def test_persisted_calendar_endpoint_survives_close_reopen_without_fetch(tmp_path):
    from tests.services.test_economics import StubProvider

    path = tmp_path / "restart.sqlite3"
    clock = FakeClock()
    first_body = None
    for opening in range(2):
        provider = StubProvider("bls", AssertionError("restart must not fetch"))
        economics = EconomicService(
            repository=SQLiteEconomicRepository(path, "live"),
            registry=registry(),
            mode="live",
            clock=clock,
            providers=[provider] if opening else [],
        )
        market = MarketStateService(
            provider=FakeMarketDataProvider(update_interval=1000, clock=clock),
            repository=DuckDBObservationRepository(":memory:"),
            clock=clock,
        )
        with TestClient(create_app(service=market, economic_service=economics)) as http:
            if not opening:
                http.portal.call(economics.accept_batch, batch())
            response = http.get("/api/v1/calendar/today")
            assert response.status_code == 200
            body = response.json()
            assert {item["id"] for item in body["events"]} == {
                "cpi-release-2026-09",
                "observed:CPI_YOY:2026-08",
            }
            scheduled = next(item for item in body["events"] if item["id"] == "cpi-release-2026-09")
            observed = next(
                item for item in body["events"] if item["id"] == "observed:CPI_YOY:2026-08"
            )
            assert scheduled["status"] == "due"
            assert scheduled["released_at"] is None
            assert scheduled["actual"] is None
            assert observed["status"] == "released"
            assert observed["actual"] == 3.2
            assert observed["consensus"] is None
            if opening:
                assert body["events"] == first_body["events"]
                assert body["providers"] == first_body["providers"]
                assert provider.calls == 0
                state = http.get("/api/v1/macro/state").json()
                assert state["series"][0]["latest"]["value"] == 3.2
            first_body = body
        with pytest.raises(RuntimeError, match="not initialized"):
            economics.repository.events()
        clock.advance(hours=1)


def test_series_success_is_independent_of_provider_health_over_http(client):
    http, economics, _ = client
    economics.clock.advance(seconds=60)
    http.portal.call(
        economics.accept_batch,
        batch(
            status="degraded",
            checked_at=economics.clock(),
            checked_series_ids=["CPI_YOY"],
            observations=[],
            events=[],
        ),
    )
    body = http.get("/api/v1/macro/state").json()
    assert body["providers"][0]["status"] == "degraded"
    assert body["series"][0]["status"] == "live"
    successful_at = body["series"][0]["last_checked_at"]
    economics.clock.advance(seconds=60)
    http.portal.call(
        economics.accept_batch,
        batch(
            status="unavailable",
            checked_at=economics.clock(),
            observations=[],
            events=[],
        ),
    )
    body = http.get("/api/v1/macro/state").json()
    assert body["series"][0]["status"] == "stale"
    assert body["series"][0]["last_checked_at"] == successful_at
    assert body["series"][0]["latest"]["value"] == 3.2


def test_publication_notices_filter_sort_and_preserve_fields(client):
    http, economics, _ = client
    notices = [
        event(
            id="notice",
            source_event_id="notice",
            scheduled_at=None,
            released_at=T0 + timedelta(minutes=1),
            status="released",
        ),
        event(
            id="scheduled",
            source_event_id="scheduled",
            scheduled_at=T0,
            released_at=T0 + timedelta(minutes=2),
            status="released",
        ),
    ]
    http.portal.call(economics.accept_batch, batch(events=notices))
    body = http.get("/api/v1/calendar?status=released").json()
    assert [item["id"] for item in body["events"]] == [
        "observed:CPI_YOY:2026-08",
        "scheduled",
        "notice",
    ]
    notice = next(item for item in body["events"] if item["id"] == "notice")
    assert notice["scheduled_at"] is None
    assert notice["released_at"] == "2026-09-17T12:01:00Z"
    assert notice["actual"] is None
    filtered = http.get(
        "/api/v1/calendar",
        params={
            "status": "released",
            "start": "2026-09-17T12:01:00Z",
            "end": "2026-09-17T12:02:00Z",
        },
    ).json()
    assert [item["id"] for item in filtered["events"]] == ["notice"]
    coverage = " ".join(body["coverage"])
    for term in (
        "CPI", "Employment Situation", "Claims", "Census", "ISM", "publication", "consensus"
    ):
        assert term.lower() in coverage.lower()
    assert "scheduled" in coverage.lower()


def test_transitions_over_socket(client):
    http, economics, _ = client
    with http.websocket_connect("/ws/market?topics=macro") as ws:
        ws.receive_json()
        future = event(id="future", source_event_id="future", scheduled_at=T0 + timedelta(days=1))
        http.portal.call(economics.accept_batch, batch(events=[future]))
        assert ws.receive_json()["type"] == "economic_event_upcoming"
        economics.clock.now += timedelta(days=1)
        http.portal.call(economics.tick)
        assert ws.receive_json()["type"] == "economic_event_due"


def test_canonical_occurrence_and_every_old_alias_return_all_source_history(client):
    from tests.services.test_calendar_projection import adp

    http, service, _ = client
    for minute in (0, 1, 2):
        http.portal.call(service.accept_batch,
                         batch(provider="consensus", observations=[], events=[adp(minute)]))
    canonical = next(item for item in service.calendar_events() if item.event_code == "ADP")
    for identity in [canonical.id, *canonical.occurrence_aliases]:
        response = http.get(f"/api/v1/calendar/{identity}")
        assert response.status_code == 200
        result = response.json()
        assert result["event"]["id"] == canonical.id
        assert {version["id"] for version in result["versions"]} == {
            "adp-0", "adp-1", "adp-2"
        }


def test_calendar_history_contract_does_not_claim_complete_forecasts(client):
    http, _, _ = client
    body = http.get('/api/v1/calendar').json()
    coverage = body['history_coverage']
    assert coverage['scope'] == 'observed_source_records'
    assert coverage['consensus_history'] == 'partial_observed_only'
    assert coverage['original_vintages'] == 'not_reconstructed'
    assert coverage['stored_schedule_count'] >= 0
    assert 'not reconstructed' in ' '.join(body['coverage'])


def test_cpi_monthly_and_annual_display_metrics_remain_separate():
    official = event(id="official", provider="fred_release_calendar", source_event_id="cpi")
    monthly = event(id="monthly", name="CPI m/m", provider="consensus", unit="percent")
    yearly = event(id="yearly", name="CPI y/y", provider="consensus", unit="percent")
    core_monthly = event(id="core-monthly", event_code="CORE_CPI", name="Core CPI m/m",
                         provider="consensus", unit="percent")
    core_yearly = event(id="core-yearly", event_code="CORE_CPI", name="Core CPI y/y",
                        provider="consensus", unit="percent")
    groups = calendar_display_groups([official, monthly, yearly, core_monthly, core_yearly])
    assert len(groups) == 4
    assert next(g.event_ids for g in groups if monthly.id in g.event_ids) == ["monthly", "official"]
    assert next(g.event_ids for g in groups if yearly.id in g.event_ids) == ["yearly"]
    assert next(g.event_ids for g in groups if core_yearly.id in g.event_ids) == ["core-yearly"]


@pytest.mark.parametrize("healthy_schedule", [True, False])
def test_optional_issuer_failure_is_local_but_no_healthy_schedule_is_stale(
    client, healthy_schedule
):
    from core.providers.economic_base import EconomicBatch

    http, economics, _ = client
    http.portal.call(
        economics.accept_batch,
        EconomicBatch(
            provider="consensus",
            checked_at=T0,
            status="healthy" if healthy_schedule else "unavailable",
            events=[event(id="slot", provider="consensus", name="PPI m/m", event_code="PPI")],
        ),
    )
    http.portal.call(
        economics.accept_batch,
        EconomicBatch(
            provider="census_retail",
            checked_at=T0,
            status="unavailable",
            issues=["HTTP request rejected"],
        ),
    )
    if not healthy_schedule:
        http.portal.call(
            economics.accept_batch,
            EconomicBatch(
                provider="bls",
                checked_at=T0,
                status="unavailable",
            ),
        )
    result = http.get("/api/v1/calendar").json()
    assert result["stale"] is not healthy_schedule
    assert (
        next(p["status"] for p in result["providers"] if p["name"] == "census_retail")
        == "unavailable"
    )


def test_core_retail_legacy_and_new_rows_group_without_hiding_headline():
    legacy = event(
        id="legacy-retail-code", event_code="RETAIL_SALES", name="Core Retail Sales m/m",
        provider="consensus", unit="percent", consensus=0.6, previous=1.4,
    )
    newer = legacy.model_copy(update={"id": "new-core-code", "event_code": "CORE_RETAIL_SALES"})
    headline = legacy.model_copy(update={"id": "headline", "name": "Retail Sales m/m"})
    service = EconomicService(mode="live", clock=lambda: T0)
    corrected = service.calendar_event_state(legacy)
    groups = calendar_display_groups([corrected, newer, headline])
    assert len(groups) == 2
    assert next(g.event_ids for g in groups if corrected.id in g.event_ids) == [
        "legacy-retail-code", "new-core-code",
    ]
    assert next(g.event_ids for g in groups if headline.id in g.event_ids) == ["headline"]
    assert legacy.event_code == "RETAIL_SALES"  # Original source record survives intact.


@pytest.mark.parametrize("code,title", [("PPI", "PPI m/m"), ("CORE_PPI", "Core PPI m/m")])
def test_ppi_same_monthly_metric_groups_but_unknown_metric_stays_separate(code, title):
    first = event(id="old", event_code=code, name=title, provider="consensus", unit="percent")
    second = first.model_copy(update={"id": "new"})
    annual = first.model_copy(update={"id": "annual", "name": title.replace("m/m", "y/y")})
    groups = calendar_display_groups([first, second, annual])
    assert len(groups) == 2
    assert next(g.event_ids for g in groups if first.id in g.event_ids) == ["new", "old"]
    assert next(g.event_ids for g in groups if annual.id in g.event_ids) == ["annual"]
