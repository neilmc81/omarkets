import json
from datetime import UTC, datetime

import pytest

from core.providers.economic_census_releases import SERIES, CensusReleasesProvider, parse_series

NOW = datetime(2026, 10, 29, 13, 0, tzinfo=UTC)
DURABLE, CORE, HOMES = SERIES


def advm3(rows):
    header = [
        "cell_value",
        "seasonally_adj",
        "error_data",
        "time_slot_id",
        "data_type_code",
        "category_code",
        "time",
        "us",
    ]
    return json.dumps(
        [header, *([v, "yes", "no", "0", "MPCNO", c, t, "1"] for v, c, t in rows)]
    ).encode()


def test_durable_goods_latest_complete_month_with_prior():
    body = advm3([("0.1", "MDM", "2026-08"), ("1.8", "MDM", "2026-09")])
    event = parse_series(DURABLE, body, NOW)
    assert (event.period, event.actual, event.previous, event.released_at) == (
        "2026-09",
        1.8,
        0.1,
        None,
    )


def test_new_home_sales_keeps_only_the_national_row_in_units():
    header = [
        "cell_value",
        "seasonally_adj",
        "error_data",
        "time_slot_id",
        "data_type_code",
        "category_code",
        "geo_level_code",
        "time",
    ]
    rows = [
        [v, "yes", "no", "0", "TOTAL", "ASOLD", geo, t]
        for v, geo, t in [
            ("680", "US", "2026-08"),
            ("676", "US", "2026-09"),
            ("413", "SO", "2026-09"),
        ]
    ]
    event = parse_series(HOMES, json.dumps([header, *rows]).encode(), NOW)
    assert (event.actual, event.previous, event.unit) == (676000.0, 680000.0, "value")


@pytest.mark.parametrize(
    "rows",
    [
        [("0.1", "MDM", "2026-08"), ("1.8", "DXT", "2026-09")],  # wrong series
        [("0.1", "MDM", "2026-09"), ("1.8", "MDM", "2026-10")],  # incomplete month
        [("0.1", "MDM", "2026-09"), ("1.8", "MDM", "2026-09")],  # duplicate month
        [("1.8", "MDM", "2026-09")],  # no prior month
    ],
)
def test_unexpected_tables_fail_closed(rows):
    with pytest.raises(ValueError):
        parse_series(DURABLE, advm3(rows), NOW)


async def test_missing_key_sends_no_request():
    batch = await CensusReleasesProvider().collect(NOW)
    assert batch.status == "not_configured" and batch.events == []
