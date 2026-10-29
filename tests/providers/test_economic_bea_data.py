import json
from datetime import UTC, datetime

import pytest

from core.providers.economic_bea_data import METRICS, BEADataProvider, parse_table, revised_on

NOW = datetime(2026, 10, 29, 13, 0, tzinfo=UTC)
GDP = [m for m in METRICS if m.table == "T10101"]
PCE = [m for m in METRICS if m.table == "T20807"]


def body(table, rows, revised="October 29, 2026"):
    return json.dumps(
        {
            "BEAAPI": {
                "Results": {
                    "Notes": [{"NoteRef": table, "NoteText": f"Table - LastRevised: {revised}"}],
                    "Data": [
                        {
                            "TableName": table,
                            "LineNumber": line,
                            "LineDescription": desc,
                            "TimePeriod": period,
                            "DataValue": value,
                        }
                        for line, desc, period, value in rows
                    ],
                }
            }
        }
    ).encode()


def test_gdp_latest_quarter_with_prior_and_revision_date():
    rows = [
        ("1", "Gross domestic product", "2026Q2", "2.2"),
        ("1", "Gross domestic product", "2026Q3", "3.4"),
        ("2", "Personal consumption expenditures", "2026Q3", "9.9"),
    ]
    [event] = parse_table(body("T10101", rows), GDP, NOW)
    assert (event.event_code, event.period, event.actual, event.previous) == (
        "GDP",
        "2026-Q3",
        3.4,
        2.2,
    )
    assert event.released_at is None
    assert revised_on(event).isoformat() == "2026-10-29"


def test_core_and_headline_pce_come_from_their_own_lines():
    rows = [
        ("1", "Personal consumption expenditures (PCE)", "2026M09", "0.5"),
        ("25", "PCE excluding food and energy", "2026M08", "0.3"),
        ("25", "PCE excluding food and energy", "2026M09", "0.3"),
    ]
    events = {e.event_code: e for e in parse_table(body("T20807", rows), PCE, NOW)}
    assert events["PCE"].actual == 0.5 and events["PCE"].period == "2026-09"
    assert events["CORE_PCE"].actual == 0.3 and events["CORE_PCE"].previous == 0.3


@pytest.mark.parametrize(
    "rows,revised",
    [
        ([("1", "Something else", "2026Q3", "3.4")], "October 29, 2026"),
        ([("1", "Gross domestic product", "2026Q3", "3.4")], "October 30, 2026"),
        ([("1", "Gross domestic product", "2026Q3", "(NA)")], "October 29, 2026"),
        ([("1", "Gross domestic product", "2026-Q3", "3.4")], "October 29, 2026"),
    ],
)
def test_unexpected_tables_fail_closed(rows, revised):
    with pytest.raises(ValueError):
        parse_table(body("T10101", rows, revised), GDP, NOW)


async def test_missing_key_sends_no_request():
    batch = await BEADataProvider().collect(NOW)
    assert batch.status == "not_configured" and batch.events == []
