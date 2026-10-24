import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from core.providers.economic_bls import BLS_INPUT_UNITS, BLSEconomicProvider
from core.providers.economic_calendars import BEACalendarProvider, FedCalendarProvider, fed_datetime
from core.providers.economic_demo import DemoEconomicProvider
from core.providers.economic_factory import build_economic_providers
from core.providers.economic_fred import FREDEconomicProvider
from core.providers.economic_nyfed import NYFedEconomicProvider
from core.providers.economic_releases import FedReleasesProvider

NOW = datetime(2026, 9, 17, 15, tzinfo=UTC)


def bls_payload():
    return {
        "status": "REQUEST_SUCCEEDED",
        "message": [],
        "Results": {
            "series": [
                {
                    "seriesID": source,
                    "data": [
                        {
                            "year": "2026",
                            "period": "M08",
                            "value": "110",
                            "footnotes": [{"code": "P", "text": "preliminary"}],
                        },
                        {"year": "2025", "period": "M08", "value": "100", "footnotes": [{}]},
                        {"year": "2026", "period": "M07", "value": "100", "footnotes": [{}]},
                    ],
                }
                for source in BLS_INPUT_UNITS
            ]
        },
    }


async def test_bls_rejects_future_reference_month():
    payload = bls_payload()
    payload["Results"]["series"][0]["data"][0]["year"] = "2027"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        batch = await BLSEconomicProvider(client, clock=lambda: NOW).fetch()
    assert batch.status == "degraded"
    assert not any(o.id == "CPI_MOM" for o in batch.observations)


async def test_bls_batch_cache_missing_revision_and_borrowed_client():
    calls = []
    now = [NOW]
    payload = bls_payload()

    def handler(request):
        calls.append(request)
        assert request.method == "POST"
        assert len(json.loads(request.content)["seriesid"]) == len(BLS_INPUT_UNITS)
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = BLSEconomicProvider(client, clock=lambda: now[0])
        first = await provider.fetch()
        assert first.status == "healthy"
        assert len(first.observations) == 17
        assert all(o.value is not None for o in first.observations)
        assert all(
            len(o.metadata.inputs) == 2
            for o in first.observations
            if o.metadata.transform != "identity"
        )
        assert len(first.checked_series_ids) == 13
        assert not first.events
        assert first.observations[0].metadata.inputs[0].source_revision
        assert all(o.released_at is None for o in first.observations)
        now[0] += timedelta(hours=1)
        refreshed = await provider.fetch()
        assert refreshed is not first
        assert refreshed.checked_at == now[0]
        assert first.checked_at == NOW
        assert len(calls) == 2
        now[0] += timedelta(days=1)
        payload["Results"]["series"][0]["data"][0]["value"] = "-"
        second = await provider.fetch()
        assert second.status == "degraded"
        assert (
            next(
                o for o in second.observations if o.id == "CPI_MOM" and o.period == "2026-08"
            ).value
            is None
        )
        assert (
            next(o for o in first.observations if o.id == "CPI_MOM" and o.period == "2026-08").value
            == 10
        )
        assert "CPI_MOM" in second.checked_series_ids
        await provider.aclose()
        assert not client.is_closed


@pytest.mark.parametrize("status,attempts", [(403, 1), (400, 1), (429, 2), (503, 2)])
async def test_retry_status(status, attempts):
    calls, sleeps = [], []

    async def sleep(seconds):
        sleeps.append(seconds)

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"Retry-After": "2"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await BLSEconomicProvider(client, clock=lambda: NOW, sleep=sleep).fetch()
    assert batch.status == "unavailable"
    assert len(calls) == attempts
    assert sleeps == ([2] if attempts == 2 else [])


async def test_long_retry_after_does_not_retry_early():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "600"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert (
            await BLSEconomicProvider(client, clock=lambda: NOW).fetch()
        ).status == "unavailable"
    assert len(calls) == 1


@pytest.mark.parametrize("body", [b"", b"<html>blocked</html>", b"{}", b"x" * 2_000_001])
async def test_invalid_or_oversized_body(body):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    ) as client:
        assert (
            await BLSEconomicProvider(client, clock=lambda: NOW).fetch()
        ).status == "unavailable"


async def test_nyfed_effective_dates_and_missing_field():
    payload = {
        "refRates": [
            {
                "type": "EFFR",
                "effectiveDate": "2026-09-16",
                "percentRate": 3.63,
                "targetRateFrom": 3.5,
                "targetRateTo": 3.75,
                "revisionIndicator": "Y",
            },
            {"type": "SOFR", "effectiveDate": "2026-09-16", "percentRate": 3.62},
        ]
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        batch = await NYFedEconomicProvider(client, clock=lambda: NOW).fetch()
        assert batch.status == "healthy"
        assert {o.id for o in batch.observations} == {
            "EFFR",
            "SOFR",
            "FED_TARGET_LOWER",
            "FED_TARGET_UPPER",
        }
        assert all(o.period == "2026-09-16" and o.released_at is None for o in batch.observations)
        del payload["refRates"][0]["targetRateTo"]
        batch = await NYFedEconomicProvider(client, clock=lambda: NOW).fetch()
        assert batch.status == "degraded"
        assert batch.checked_series_ids == ["EFFR", "SOFR", "FED_TARGET_LOWER"]
        assert not any(o.id == "FED_TARGET_UPPER" for o in batch.observations)


def ics(start="20260930T123000Z", sequence=0, summary="Personal Income and Outlays, August 2026"):
    return "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:tests",
            "BEGIN:VEVENT",
            "UID:stable-uid",
            f"SUMMARY:{summary}",
            f"DTSTART:{start}",
            "DTSTAMP:20260901T120000Z",
            f"SEQUENCE:{sequence}",
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        ]
    )


async def test_bea_uid_survives_reschedule_and_multimonth_period():
    body = [ics()]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body[0]))
    ) as client:
        first = (await BEACalendarProvider(client, clock=lambda: NOW).fetch()).events[0]
        assert first.period == "2026-08" and first.event_code == "PCE"
        assert first.actual is None and first.consensus is None and first.released_at is None
        body[0] = ics(
            "20261001T123000Z", 1, "Personal Income and Outlays, October and November 2025"
        )
        second = (await BEACalendarProvider(client, clock=lambda: NOW).fetch()).events[0]
        assert first.id == second.id
        assert second.period is None
        assert second.source_revision == "1"
        assert first.scheduled_at != second.scheduled_at


async def test_bea_rejects_naive_timestamp():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=ics("20260930T083000"))
        )
    ) as client:
        batch = await BEACalendarProvider(client, clock=lambda: NOW).fetch()
        assert batch.status == "degraded" and not batch.events


def test_fed_dst_and_ambiguous_times():
    assert fed_datetime("2026-10", "28", "2:00 p.m.").hour == 18
    assert fed_datetime("2026-12", "9", "2:00 p.m.").hour == 19
    for month, day, time in [
        ("2026-11", "1", "1:30 a.m."),
        ("2026-03", "8", "2:30 a.m."),
        ("2026-09", "1", ""),
    ]:
        with pytest.raises(ValueError):
            fed_datetime(month, day, time)


async def test_fed_schedule_does_not_infer_sep_actual_or_period():
    rows = [
        {"title": "FOMC Meeting", "month": "2026-10", "days": "28", "time": "2:00 p.m."},
        {"title": "Beige Book", "month": "2026-10", "days": "14", "time": ""},
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"events": rows}))
    ) as client:
        first = await FedCalendarProvider(client, clock=lambda: NOW).fetch()
        assert first.status == "degraded" and len(first.events) == 1
        event = first.events[0]
        assert event.period is None and event.actual is None and event.event_code == "FOMC"
        rows[0]["days"] = "29"
        second = await FedCalendarProvider(client, clock=lambda: NOW).fetch()
        assert second.events[0].id == event.id


async def test_rss_publication_and_entity_rejection():
    body = [
        """<rss><channel><item><title>Federal Reserve releases economic projections</title>
    <link>https://www.federalreserve.gov/newsevents/pressreleases/monetary20260916b.htm</link>
    <guid>sep-2026-09</guid><pubDate>Wed, 16 Sep 2026 18:00:00 GMT</pubDate>
    </item></channel></rss>"""
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body[0]))
    ) as client:
        batch = await FedReleasesProvider(client, clock=lambda: NOW).fetch()
        event = batch.events[0]
        assert event.event_code == "SEP" and event.status == "released"
        assert event.scheduled_at is None and event.actual is None
        assert event.released_at == datetime(2026, 9, 16, 18, tzinfo=UTC)
        body[0] = '<!DOCTYPE rss [<!ENTITY x "bad">]><rss>&x;</rss>'
        assert (
            await FedReleasesProvider(client, clock=lambda: NOW).fetch()
        ).status == "unavailable"


async def test_fred_no_key_no_request_and_sanitized_error():
    from core.domain.macro_registry import MACRO_DEFINITIONS

    fred_definitions = [item for item in MACRO_DEFINITIONS if item.provider == "fred"]
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("secret-key-containing-url", request=request)

    async def sleep(seconds):
        pass

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await FREDEconomicProvider(client, clock=lambda: NOW).fetch()
        assert batch.status == "not_configured" and not calls
        assert batch.checked_series_ids == []
        batch = await FREDEconomicProvider(
            client, clock=lambda: NOW, api_key="secret", sleep=sleep
        ).fetch()
        assert batch.status == "degraded"
        assert "secret" not in batch.model_dump_json()
        assert batch.checked_series_ids == []
        assert len(calls) == len(fred_definitions) * 2


FRED_MAINTENANCE_PAGE = (
    Path(__file__).parents[1] / "fixtures/economics/fred-maintenance-2026-10-22.html"
).read_bytes()


async def test_fred_maintenance_page_is_reported_once_without_more_requests():
    # Captured Oct 22, 2026: api.stlouisfed.org answered HTTP 200 with this page.
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=FRED_MAINTENANCE_PAGE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await FREDEconomicProvider(client, clock=lambda: NOW, api_key="dummy-key").fetch()
    assert batch.status == "degraded"
    assert batch.issues == [
        "FRED is down for maintenance (St. Louis Fed holding page); retried next poll."
    ]
    assert batch.observations == [] and batch.checked_series_ids == []
    assert len(calls) == 1


def fred_payload():
    return {
        "observations": [
            {
                "date": "2026-08-01",
                "value": "110",
                "realtime_start": "2026-09-17",
                "realtime_end": "2026-09-17",
            },
            {
                "date": "2026-07-01",
                "value": "100",
                "realtime_start": "2026-09-17",
                "realtime_end": "2026-09-17",
            },
            {
                "date": "2026-06-01",
                "value": "100",
                "realtime_start": "2026-09-17",
                "realtime_end": "2026-09-17",
            },
        ]
    }


async def test_fred_successful_parse_emits_history_and_checked_ids():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=fred_payload())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await FREDEconomicProvider(client, clock=lambda: NOW, api_key="dummy-key").fetch()
    assert batch.status == "degraded"
    assert batch.checked_series_ids == [
        "RETAIL_SALES",
        "CORE_RETAIL_SALES",
        "INDUSTRIAL_PRODUCTION",
        "INITIAL_CLAIMS",
        "CONTINUING_CLAIMS",
        "PPI",
        "CORE_PPI",
        "HOUSING_STARTS",
        "BUILDING_PERMITS",
        "NEW_HOME_SALES",
        "DURABLE_GOODS",
        "EMPIRE_STATE_MFG",
        "PHILLY_FED_MFG",
    ]
    assert "GDP: request or validation failed" in batch.issues
    assert [o.period for o in batch.observations if o.id == "RETAIL_SALES"] == [
        "2026-07",
        "2026-08",
    ]
    assert [o.value for o in batch.observations if o.id == "RETAIL_SALES"] == [0, 10]
    assert not any(o.id in {"PCE_YOY", "CORE_PCE_YOY"} for o in batch.observations)
    assert "PCE_YOY: value unavailable" in batch.issues
    assert "CORE_PCE_YOY: value unavailable" in batch.issues
    assert all(o.released_at is None for o in batch.observations)
    assert all(
        source.source_revision is None for o in batch.observations for source in o.metadata.inputs
    )
    assert any("query window" in note for o in batch.observations for note in o.notes)


@pytest.mark.parametrize("response_kind", ["success", "http_error", "transport_error"])
async def test_fred_log_redaction_success_and_error(caplog, response_kind):
    import logging

    calls = []

    async def sleep(seconds):
        pass

    def handler(request):
        calls.append(request)
        logging.getLogger("httpcore.http11").debug("request %s", request.url)
        if response_kind == "transport_error":
            raise httpx.ConnectError("secret-key-containing-url", request=request)
        if response_kind == "http_error":
            return httpx.Response(500)
        return httpx.Response(200, json=fred_payload())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with caplog.at_level(logging.DEBUG):
            batch = await FREDEconomicProvider(
                client, clock=lambda: NOW, api_key="dummy-key", sleep=sleep
            ).fetch()
            logging.getLogger("httpx").info("unrelated request after FRED")
    assert calls
    assert all(request.url.params["api_key"] == "dummy-key" for request in calls)
    assert "Sensitive economic HTTP operation" in caplog.text
    assert "unrelated request after FRED" in caplog.text
    assert "dummy-key" not in caplog.text
    assert "secret-key-containing-url" not in caplog.text
    assert "dummy-key" not in batch.model_dump_json()


@pytest.mark.parametrize("provider_type", [FREDEconomicProvider, FedReleasesProvider])
async def test_adapter_refreshes_at_service_polling_interval(provider_type):
    from unittest.mock import AsyncMock

    from core.providers.economic_base import EconomicBatch
    from core.services.economics import EconomicPolling

    now = [NOW]
    provider = provider_type(clock=lambda: now[0])
    provider.collect = AsyncMock(
        side_effect=lambda instant: EconomicBatch(
            provider=provider.name,
            checked_at=instant,
            status="healthy",
        )
    )
    first = await provider.fetch()
    now[0] += timedelta(seconds=EconomicPolling().interval(provider.name))
    second = await provider.fetch()
    assert provider.collect.await_count == 2
    assert second.checked_at == now[0]
    assert first.checked_at == NOW


@pytest.mark.parametrize("failure", ["transport", "retry_date"])
async def test_http_transient_failure_recovers_once(failure):
    calls, sleeps = [], []

    async def sleep(seconds):
        sleeps.append(seconds)

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            if failure == "transport":
                raise httpx.ConnectError("offline", request=request)
            return httpx.Response(503, headers={"Retry-After": "Thu, 17 Sep 2026 15:00:07 GMT"})
        return httpx.Response(200, json=bls_payload())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await BLSEconomicProvider(client, clock=lambda: NOW, sleep=sleep).fetch()
    assert batch.status == "healthy"
    assert len(calls) == 2
    assert sleeps == ([1] if failure == "transport" else [7])
    assert all(item.released_at is None for item in batch.observations)
    assert all(
        source.released_at is None for item in batch.observations for source in item.metadata.inputs
    )


async def test_past_bea_schedule_never_invents_publication():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=ics("20260916T123000Z")),
        )
    ) as client:
        result = await BEACalendarProvider(client, clock=lambda: NOW).fetch()
    assert result.status == "healthy"
    assert len(result.events) == 1
    item = result.events[0]
    assert item.status == "due"
    assert item.released_at is item.actual is item.consensus is item.raw_surprise is None
    assert item.received_at == NOW


async def test_demo_and_factory_modes():
    now = [NOW]
    provider = DemoEconomicProvider(clock=lambda: now[0])
    first = await provider.fetch()
    now[0] += timedelta(seconds=31)
    released = await provider.fetch()
    now[0] += timedelta(seconds=30)
    revised = await provider.fetch()
    assert first.events[0].status == "scheduled"
    assert released.events[0].status == "released"
    assert revised.events[0].revision == 2
    assert first.events[0].id == revised.events[0].id
    assert released.observations[0].value != revised.observations[0].value
    assert all(b.provider == "demo" for b in (first, released, revised))
    assert build_economic_providers("disabled") == []
    assert [p.name for p in build_economic_providers("demo")] == ["demo"]
    assert [p.name for p in build_economic_providers("live")] == [
        "bls",
        "nyfed",
        "bea_calendar",
        "bea_releases",
        "fed_calendar",
        "fed_releases",
        "fred_release_calendar",
        "fred",
        "consensus",
        "dol_claims",
        "ism_newswire",
        "adp_releases",
        "nar_releases",
        "census_retail",
        "fed_g17",
    ]
    with pytest.raises(ValueError):
        build_economic_providers("unknown")


def test_fed_speeches_and_testimony_are_schedule_only_events():
    from core.providers.economic_calendars import fed_speech

    now = datetime(2026, 10, 20, 12, tzinfo=UTC)
    chair = fed_speech({"type": "Testimony", "title": "Testimony - Chairman Kevin Warsh",
                        "month": "2026-11", "days": "4", "time": "10:00 a.m.",
                        "location": "Before the Committee on Banking &amp; Finance<br>"},
                       now, "fed_calendar")
    assert chair.event_code == "FED_TESTIMONY" and chair.importance == "high"
    assert chair.name == "Fed testimony: Chairman Kevin Warsh"
    assert chair.scheduled_at == datetime(2026, 11, 4, 15, 0, tzinfo=UTC)  # 10:00 EST
    assert chair.notes[-1] == "Venue: Before the Committee on Banking & Finance"
    assert chair.actual is None and chair.consensus is None
    governor = fed_speech({"type": "Speeches", "title": "Speech -- Governor Lisa D. Cook",
                           "month": "2026-10", "days": "30", "time": "3:30 p.m."},
                          now, "fed_calendar")
    assert governor.importance == "medium" and governor.name == "Fed speech: Governor Lisa D. Cook"
    vice = fed_speech({"type": "Speeches", "title": "Speech - Vice Chair Philip N. Jefferson",
                       "month": "2026-10", "days": "30", "time": "3:30 p.m."}, now, "fed_calendar")
    assert vice.importance == "medium"


@pytest.mark.parametrize(
    "row",
    [
        {"type": "Speeches", "title": "Speech - Governor X", "month": "2026-10", "days": "1",
         "time": "9:00 a.m."},  # more than a week ago
        {"type": "Speeches", "title": "Webcast of something", "month": "2026-10", "days": "30",
         "time": "9:00 a.m."},
        {"type": "Speeches", "title": "Speech - Governor X", "month": "2026-10", "days": "29,30",
         "time": "9:00 a.m."},
        {"type": "Stat", "title": "Speech - Governor X", "month": "2026-10", "days": "30",
         "time": "9:00 a.m."},
    ],
)
def test_bad_or_old_speech_rows_are_dropped(row):
    from core.providers.economic_calendars import fed_speech

    assert fed_speech(row, datetime(2026, 10, 20, 12, tzinfo=UTC), "fed_calendar") is None


async def test_speech_without_a_time_does_not_degrade_the_fed_calendar():
    rows = [
        {"title": "FOMC Meeting", "month": "2026-10", "days": "28", "time": "2:00 p.m."},
        {"type": "Speeches", "title": "Speech - Governor Lisa D. Cook", "month": "2026-10",
         "days": "30", "time": ""},
        {"type": "Speeches", "title": "Speech - Governor Lisa D. Cook", "month": "2026-10",
         "days": "30", "time": "3:30 p.m."},
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"events": rows}))
    ) as client:
        batch = await FedCalendarProvider(client, clock=lambda: NOW).fetch()
    assert batch.status == "healthy"
    assert sorted(e.event_code for e in batch.events) == ["FED_SPEECH", "FOMC"]
