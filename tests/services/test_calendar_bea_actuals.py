from datetime import UTC, datetime

from core.domain.economics import EconomicEvent
from core.services.calendar_projection import project_calendar

SLOT = datetime(2026, 10, 29, 12, 30, tzinfo=UTC)
NOW = datetime(2026, 10, 29, 13, 0, tzinfo=UTC)


def schedule(code, title, **changes):
    return EconomicEvent.model_validate(
        dict(
            id=title,
            event_code=code,
            name=title,
            category="growth",
            importance="high",
            scheduled_at=SLOT,
            provider="consensus",
            source_event_id=title,
            unit="percent",
            frequency="release",
            status="due",
            received_at=datetime(2026, 10, 27, tzinfo=UTC),
            mode="live",
            consensus=3.2,
            previous=1.5,
            source_url="https://nfs.faireconomy.media/ff_calendar_thisweek.json",
        )
        | changes
    )


def bea(code, period, actual, revised="2026-10-29", previous=2.2, received=NOW):
    identity = f"{code}:{period}:{revised}"
    return EconomicEvent.model_validate(
        dict(
            id=f"bea-{identity}",
            event_code=code,
            name=code,
            category="growth",
            importance="critical",
            unit="percent",
            frequency="quarterly",
            period=period,
            status="released",
            actual=actual,
            previous=previous,
            actual_basis="source_release",
            provider="bea_data",
            source_event_id=identity,
            source_url="https://www.bea.gov/data",
            received_at=received,
            mode="live",
        )
    )


def by_name(events):
    return {e.name: e for e in events}


def test_release_day_value_fills_the_matching_row_and_flags_revised_previous():
    rows = by_name(
        project_calendar(
            [
                schedule("GDP", "Advance GDP q/q"),
                schedule("GDP_PRICE_INDEX", "Advance GDP Price Index q/q", previous=6.2),
                bea("GDP", "2026-Q3", 3.4),
                bea("GDP_PRICE_INDEX", "2026-Q3", 2.3, previous=6.1),
            ],
            now=NOW,
        )
    )
    assert rows["Advance GDP q/q"].actual == 3.4
    assert rows["Advance GDP q/q"].revised_previous == 2.2
    assert rows["Advance GDP Price Index q/q"].actual == 2.3
    assert rows["Advance GDP Price Index q/q"].revised_previous == 6.1
    assert "GDP" not in rows  # issuer row is folded into the schedule


def test_stale_table_does_not_fill_todays_row():
    rows = by_name(
        project_calendar(
            [schedule("GDP", "Advance GDP q/q"), bea("GDP", "2026-Q2", 2.2, revised="2026-09-25")],
            now=NOW,
        )
    )
    assert rows["Advance GDP q/q"].actual is None


def test_y_on_y_rows_are_never_filled_from_m_on_m_values():
    rows = by_name(
        project_calendar(
            [
                schedule("CORE_PCE", "Core PCE Price Index y/y"),
                bea("CORE_PCE", "2026-09", 0.3),
            ],
            now=NOW,
        )
    )
    assert rows["Core PCE Price Index y/y"].actual is None


def test_value_fetched_before_the_scheduled_time_is_ignored():
    early = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    rows = by_name(
        project_calendar(
            [schedule("GDP", "Advance GDP q/q"), bea("GDP", "2026-Q3", 3.4, received=early)],
            now=NOW,
        )
    )
    assert rows["Advance GDP q/q"].actual is None


def census(code, period, actual, unit="percent", previous=0.1, received=NOW):
    identity = f"{period}:{code}"
    return EconomicEvent.model_validate(
        dict(
            id=f"census-{identity}",
            event_code=code,
            name=code,
            category="growth",
            importance="medium",
            unit=unit,
            frequency="monthly",
            period=period,
            status="released",
            actual=actual,
            previous=previous,
            actual_basis="source_release",
            provider="census_releases",
            source_event_id=identity,
            source_url="https://www.census.gov/",
            received_at=received,
            mode="live",
        )
    )


def test_census_values_fill_rows_for_the_month_the_release_covers():
    tuesday = datetime(2026, 10, 27, 12, 30, tzinfo=UTC)
    rows = by_name(
        project_calendar(
            [
                schedule(
                    "DURABLE_GOODS",
                    "Durable Goods Orders m/m",
                    scheduled_at=tuesday,
                    consensus=2.0,
                    previous=0.0,
                ),
                schedule(
                    "DURABLE_GOODS",
                    "Core Durable Goods Orders m/m",
                    scheduled_at=tuesday,
                    consensus=0.6,
                    previous=0.3,
                ),
                schedule(
                    "NEW_HOME_SALES",
                    "New Home Sales",
                    unit="value",
                    scheduled_at=tuesday.replace(hour=14),
                    consensus=651000.0,
                    previous=684000.0,
                ),
                census("DURABLE_GOODS", "2026-09", 1.8),
                census("CORE_DURABLE_GOODS", "2026-09", 0.5, previous=0.3),
                census("NEW_HOME_SALES", "2026-09", 676000.0, unit="value", previous=680000.0),
            ],
            now=NOW,
        )
    )
    assert rows["Durable Goods Orders m/m"].actual == 1.8
    assert rows["Durable Goods Orders m/m"].revised_previous == 0.1
    assert rows["Core Durable Goods Orders m/m"].actual == 0.5
    assert rows["Core Durable Goods Orders m/m"].revised_previous is None
    assert rows["New Home Sales"].actual == 676000.0


def test_census_value_for_the_wrong_month_is_ignored():
    rows = by_name(
        project_calendar(
            [
                schedule("DURABLE_GOODS", "Durable Goods Orders m/m"),
                census("DURABLE_GOODS", "2026-08", 0.1),
            ],
            now=NOW,
        )
    )
    assert rows["Durable Goods Orders m/m"].actual is None


def published(provider, code, actual, at, unit, **extra):
    return EconomicEvent.model_validate(
        dict(
            id=f"{provider}-{code}",
            event_code=code,
            name=code,
            category="policy",
            importance="critical",
            unit=unit,
            frequency="release",
            status="released",
            actual=actual,
            actual_basis="source_release",
            provider=provider,
            source_event_id=f"{provider}-{code}",
            source_url="https://www.federalreserve.gov/",
            released_at=at,
            received_at=NOW,
            mode="live",
        )
        | extra
    )


def test_cb_consumer_confidence_fills_from_the_release_in_its_slot():
    slot = datetime(2026, 10, 27, 14, 0, tzinfo=UTC)
    rows = by_name(
        project_calendar(
            [
                schedule(
                    "CONSUMER_CONFIDENCE",
                    "CB Consumer Confidence",
                    unit="value",
                    scheduled_at=slot,
                    consensus=81.2,
                    previous=81.9,
                ),
                published(
                    "conference_board",
                    "CONSUMER_CONFIDENCE",
                    91.7,
                    slot,
                    "value",
                    previous=84.2,
                    period="2026-10",
                    frequency="monthly",
                ),
            ],
            now=NOW,
        )
    )
    assert rows["CB Consumer Confidence"].actual == 91.7
    assert rows["CB Consumer Confidence"].revised_previous == 84.2


def test_fed_funds_rate_fills_from_the_statement_and_the_statement_row_stays():
    slot = datetime(2026, 10, 28, 18, 0, tzinfo=UTC)
    rows = by_name(
        project_calendar(
            [
                schedule(
                    "FOMC", "Federal Funds Rate", scheduled_at=slot, consensus=4.0, previous=4.0
                ),
                published("fed_releases", "FOMC_STATEMENT", 4.0, slot, "percent"),
            ],
            now=NOW,
        )
    )
    assert rows["Federal Funds Rate"].actual == 4.0
    assert rows["FOMC_STATEMENT"].actual == 4.0


def test_statement_from_another_meeting_does_not_fill_the_rate():
    slot = datetime(2026, 10, 28, 18, 0, tzinfo=UTC)
    rows = by_name(
        project_calendar(
            [
                schedule("FOMC", "Federal Funds Rate", scheduled_at=slot),
                published(
                    "fed_releases",
                    "FOMC_STATEMENT",
                    4.25,
                    datetime(2026, 9, 16, 18, 0, tzinfo=UTC),
                    "percent",
                ),
            ],
            now=NOW,
        )
    )
    assert rows["Federal Funds Rate"].actual is None
