"""What to watch: cards before high-importance releases, with rules of thumb and history."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from core.desk import watch as W
from core.desk.models import (
    DeskNote,
    NoteDraft,
    NoteKind,
    Playbook,
    PlaybookBucket,
    PlaybookIndicator,
)
from core.desk.service import DeskService, DeskSources
from core.desk.store import DeskStore

# Thursday Oct 22 2026, 08:30 ET.
AT = datetime(2026, 10, 22, 12, 30, tzinfo=UTC)
AFTER = timedelta(minutes=50)


def row(code="INITIAL_CLAIMS", name="Unemployment Claims", importance="high", actual=None,
        consensus=200_000.0, at=AT):
    return SimpleNamespace(event_code=code, name=name, scheduled_at=at, actual=actual,
                           consensus=consensus, previous=194_000.0, unit="count",
                           importance=importance, status="scheduled", country="US",
                           statement=None)


def playbook(name="Unemployment Claims", count=3, enough=False):
    bucket = PlaybookBucket(verdict="below", count=count, first_up=0, first_down=1,
                            first_flat=2, close_up=1, close_down=2, close_flat=0,
                            close_missing=0, text="Below forecast 3 times. ...")
    return Playbook(generated_at=AT, since=AT, min_releases=5, indicators=[
        PlaybookIndicator(name=name, count=count, enough=enough, text="", buckets=[bucket],
                          releases=[])])


def test_readings_cover_code_variants_and_families():
    assert W.reading("CPI_MOM").family == "inflation"
    assert W.reading("CORE_PCE_YOY").family == "inflation"
    assert W.reading("GDP_PRICE_INDEX").family == "inflation"
    assert W.reading("GDP").family == "growth"
    assert W.reading("INITIAL_CLAIMS").family == "jobless"
    assert W.reading("FOMC_PRESS_CONFERENCE").family == "fed"
    assert W.reading("TRADE") is None


def test_card_shows_from_two_hours_before_with_forecast_reading_and_history():
    rows = [row()]
    assert W.cards(rows, None, now=AT - timedelta(hours=2, minutes=1),
                   until_written=AFTER) == []
    [card] = W.cards(rows, playbook(), now=AT - timedelta(hours=1), until_written=AFTER)
    assert card.title == "Jobless claims" and not card.released
    assert card.explainer.startswith("Weekly count")
    [figure] = card.figures
    assert (figure.forecast_text, figure.previous_text, figure.actual_text) == ("200K", "194K",
                                                                                 None)
    assert figure.history == ["Below forecast 3 times. ..."]
    assert figure.history_note == "Only 3 earlier releases: examples, not a pattern."
    assert [r.family for r in card.readings] == ["jobless"]


def test_card_turns_released_then_goes_when_the_note_exists_or_too_late():
    rows = [row(actual=210_000.0)]
    [card] = W.cards(rows, None, now=AT + timedelta(minutes=5), until_written=AFTER)
    assert card.released and card.figures[0].actual_text == "210K"
    assert card.figures[0].verdict == "above" and card.figures[0].history == []
    assert W.cards(rows, None, now=AT + timedelta(minutes=5), until_written=AFTER,
                   written=[card.key]) == []
    assert W.cards(rows, None, now=AT + AFTER + timedelta(minutes=1), until_written=AFTER) == []


def test_medium_releases_and_fed_speeches_get_no_card():
    rows = [row(code="HOUSING_STARTS", name="Housing Starts", importance="medium"),
            row(code="FED_SPEECH", name="Powell speaks", importance="high", consensus=None,
                at=AT + timedelta(minutes=30))]
    assert W.cards(rows, None, now=AT - timedelta(hours=1), until_written=AFTER) == []


def test_mixed_release_gets_one_reading_per_family():
    rows = [row(), row(code="CPI_MOM", name="CPI m/m", importance="critical", consensus=0.3),
            row(code="CORE_CPI_MOM", name="Core CPI m/m", importance="critical",
                consensus=0.3)]
    [card] = W.cards(rows, None, now=AT - timedelta(minutes=30), until_written=AFTER)
    assert [r.family for r in card.readings] == ["inflation", "jobless"]


async def test_desk_view_carries_cards_and_drops_them_once_the_note_is_written():
    clock = {"now": AT - timedelta(minutes=30)}

    def calendar(start, end):
        return [r for r in [row()] if start <= r.scheduled_at < end]

    store = DeskStore(":memory:")
    service = DeskService(
        sources=DeskSources(market_state=lambda: SimpleNamespace(instruments={}),
                            history=lambda *a, **k: [], calendar_events=calendar,
                            news_events=lambda: []),
        store=store, writer=SimpleNamespace(ready=True, detail=None),
        clock=lambda: clock["now"],
    )
    view = await service.view()
    assert [c.title for c in view.watch] == ["Jobless claims"]

    clock["now"] = AT + timedelta(minutes=27)
    draft = NoteDraft(headline="Claims fell", summary="sum", what_it_means="means",
                      market_reaction="react", rates_read="rates", learn="lesson",
                      nq_tone="neutral")
    store.save(DeskNote(id=f"release:{view.watch[0].key}", kind=NoteKind.RELEASE,
                        title="Jobless claims", created_at=clock["now"], draft=draft))
    assert (await service.view()).watch == []
