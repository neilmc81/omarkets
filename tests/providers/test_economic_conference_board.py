from datetime import UTC, datetime

import pytest

from core.providers.economic_conference_board import parse_listing

NOW = datetime(2026, 10, 29, 13, 0, tzinfo=UTC)


def card(stamp, title, summary, slug="us-consumer-confidence-increased-in-october-302920731"):
    return f"""<a class="newsreleaseconsolidatelink" href="/news-releases/{slug}.html">
      <div><img alt="x"></div><div class="card"><h3>
        <small>{stamp} ET</small>
        {title}
      </h3>
      <p class="remove-outline">{summary}</p></div></a>"""


OCTOBER = card(
    "Oct 27, 2026, 10:00",
    "US Consumer Confidence Increased in October",
    "The Conference Board Consumer Confidence Index&reg; rose by 7.5 points to 91.7 "
    "(1985=100) in October, up from 84.2 in September (a slight upward...",
)


def test_headline_level_revised_prior_and_publication_time():
    page = (
        OTHER := card("Oct 27, 2026, 15:22", "Report: Free Enterprise", "Americans...")
    ) + OCTOBER
    assert OTHER
    [event] = parse_listing(page.encode(), NOW)
    assert (event.period, event.actual, event.previous) == ("2026-10", 91.7, 84.2)
    assert event.released_at == datetime(2026, 10, 27, 14, 0, tzinfo=UTC)
    assert event.source_url.startswith("https://www.prnewswire.com/news-releases/")


def test_december_release_in_january_belongs_to_the_prior_year():
    page = card(
        "Jan 27, 2026, 10:00",
        "US Consumer Confidence Fell in January",
        "The Conference Board Consumer Confidence Index® fell by 1.0 points to 90.0 "
        "(1985=100) in January, down from 91.0 in December.",
    )
    [event] = parse_listing(page.encode(), NOW)
    assert event.period == "2026-01"


@pytest.mark.parametrize(
    "summary",
    [
        "Consumer confidence improved in October.",  # no number
        "The Conference Board Consumer Confidence Index® rose to 91.7 (1985=100) in October, "
        "up from 84.2 in August.",  # prior month is not the month before
    ],
)
def test_unrecognised_headline_fails_closed(summary):
    page = card("Oct 27, 2026, 10:00", "US Consumer Confidence Increased in October", summary)
    with pytest.raises(ValueError):
        parse_listing(page.encode(), NOW)
