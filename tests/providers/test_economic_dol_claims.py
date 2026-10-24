from datetime import UTC, datetime

import pytest

from core.providers.economic_dol_claims import parse_claims

TEXT = """TRANSMISSION OF MATERIALS IN THIS RELEASE IS EMBARGOED UNTIL
8:30 A.M. (Eastern) Thursday, September 24, 2026
UNEMPLOYMENT INSURANCE WEEKLY CLAIMS
SEASONALLY ADJUSTED DATA
In the week ending September 19, the advance figure for seasonally adjusted
initial claims was 197,000, a decrease of 1,000 from the previous week's revised level.
The previous week's level was revised up by 2,000 from 196,000 to 198,000.
"""


def test_exact_source_publication_and_reference_week_not_today():
    event = parse_claims(TEXT, datetime(2026, 10, 1, 11, tzinfo=UTC))
    assert event.actual == 197000
    assert event.previous == 196000
    assert event.revised_previous == 198000
    assert event.period == "2026-09-19"
    assert event.released_at == datetime(2026, 9, 24, 12, 30, tzinfo=UTC)
    assert event.actual_basis == "source_release"


def test_embargoed_release_not_available():
    with pytest.raises(ValueError, match="embargoed"):
        parse_claims(TEXT, datetime(2026, 9, 24, 12, 29, tzinfo=UTC))


def test_changed_format_and_unadjusted_only_fail_closed():
    with pytest.raises(ValueError):
        parse_claims(TEXT.replace("seasonally adjusted", "unadjusted"),
                     datetime(2026, 10, 1, tzinfo=UTC))


def test_year_boundary_reference_period():
    text = TEXT.replace("September 24, 2026", "January 7, 2027").replace(
        "September 19", "December 26"
    )
    assert parse_claims(text, datetime(2027, 1, 8, tzinfo=UTC)).period == "2026-12-26"
