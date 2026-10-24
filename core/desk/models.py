"""Typed records for the Omarkets desk: the plain-English notes a trader reads.

Python computes every figure, surprise and price move in a note. The language
model only writes the explanation (:class:`NoteDraft`) from those facts.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


class NoteKind(StrEnum):
    PRIMER = "primer"  # the week ahead
    PREMARKET = "premarket"  # before the US cash open
    RELEASE = "release"  # an economic data release
    FED = "fed"  # FOMC decision / press conference / minutes
    EARNINGS = "earnings"  # mega-cap company results
    MOVE = "move"  # NQ made a large move
    ASK = "ask"  # user pressed "explain the market now"
    REVIEW = "review"  # the week in review, after the week's last close
    AUCTION = "auction"  # a notably strong or weak Treasury auction


Verdict = Literal["above", "below", "in_line", "not_reported", "no_forecast"]
Tone = Literal["supportive", "negative", "mixed", "neutral"]


SurpriseSize = Literal["bigger", "typical", "smaller", "in_line", "not_enough_history"]


class Surprise(BaseModel):
    """Actual minus forecast, sized against this indicator's own past surprises."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: float
    #: e.g. "+0.1 pt", "-12K".
    value_text: str
    size: SurpriseSize
    #: Earlier releases with a forecast that the size is judged against.
    history_count: int = Field(ge=0)
    #: How many of those earlier surprises were smaller than this one.
    larger_than: int | None = None
    text: str


class Figure(BaseModel):
    """One released number, with its forecast and previous value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    unit: str
    actual: float | None = None
    forecast: float | None = None
    previous: float | None = None
    #: Decimals the figure is officially reported at; comparisons use this rounding.
    decimals: int = Field(default=1, ge=0, le=4)
    verdict: Verdict
    actual_text: str
    forecast_text: str
    previous_text: str
    surprise: Surprise | None = None


class Move(BaseModel):
    """How far one market moved between two observed quotes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    label: str
    start_value: float
    end_value: float
    start_at: AwareDatetime
    end_at: AwareDatetime
    change: float
    #: "%" for prices and indexes, "bp" (basis points) for yields.
    change_unit: Literal["%", "bp"]
    delayed: bool = False
    text: str


class Quote(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    label: str
    value: float | None
    value_text: str
    change_text: str | None
    direction: Literal["up", "down", "flat", "unknown"]
    as_of: AwareDatetime | None
    delayed: bool
    stale: bool
    #: The value is a last close, not a stale feed: the market is shut
    #: (weekend, holiday, daily break) or a cash index is outside its hours.
    closed: bool = False
    #: Short wording for a closed quote: "Markets closed" or "cash closed".
    closed_text: str | None = None
    #: What the instrument is and why it matters for NQ (rule of thumb).
    explainer: str | None = None


class YieldLine(BaseModel):
    """One Treasury yield (or the 10y-2y curve) with day and week changes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    label: str
    value: float | None
    value_text: str
    day_change_bp: float | None
    week_change_bp: float | None
    day_text: str
    week_text: str


class RatesView(BaseModel):
    """The bond-market picture. Computed in Python; the writer only explains it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: AwareDatetime | None
    lines: list[YieldLine]
    #: 10-year minus 2-year, in basis points. Negative means "inverted".
    curve_bp: float | None
    curve_text: str
    explainer: str


class Headline(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str
    title: str
    published_at: AwareDatetime | None = None
    url: str | None = None


class UpcomingItem(BaseModel):
    """Something scheduled that may move NQ."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["economic", "fed", "earnings", "auction"]
    title: str
    at: AwareDatetime | None
    #: For earnings that only report "before open"/"after close".
    when_text: str
    importance: Literal["critical", "high", "medium"]
    detail: str | None = None
    #: One-line beginner explanation of what this is.
    explainer: str | None = None


class NoteDraft(BaseModel):
    """What the language model writes. Plain English, no trade advice."""

    model_config = ConfigDict(extra="forbid")

    headline: str = Field(min_length=3, max_length=160)
    summary: str = Field(min_length=3, max_length=700)
    what_it_means: str = Field(min_length=3, max_length=2500)
    market_reaction: str = Field(min_length=3, max_length=1800)
    #: What bond yields did and why that matters for Nasdaq stocks.
    rates_read: str = Field(min_length=3, max_length=1800)
    watch_next: list[str] = Field(default_factory=list)
    learn: str = Field(min_length=3, max_length=1200)
    nq_tone: Tone

    @field_validator("watch_next")
    @classmethod
    def _at_most_four(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item.strip()][:4]


FollowUpVerdict = Literal["held", "grew", "faded", "reversed", "quiet"]


class Checkpoint(BaseModel):
    """Markets measured from the event time to one later moment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: Literal["1h", "close"]
    #: Exchange time measured to.
    at: AwareDatetime
    moves: list[Move] = Field(default_factory=list)


class FollowUp(BaseModel):
    """"Did it hold?": how NQ's first reaction aged. Deterministic, no model call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    checkpoints: list[Checkpoint] = Field(default_factory=list)
    verdict: FollowUpVerdict | None = None
    #: True once every planned checkpoint has been measured.
    final: bool = False
    text: str


class DeskNote(BaseModel):
    """A stored note: deterministic facts plus the written explanation."""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: NoteKind
    title: str
    created_at: AwareDatetime
    #: When the thing being explained happened (release time, open, move start).
    event_at: AwareDatetime | None = None
    figures: list[Figure] = Field(default_factory=list)
    moves: list[Move] = Field(default_factory=list)
    headlines: list[Headline] = Field(default_factory=list)
    upcoming: list[UpcomingItem] = Field(default_factory=list)
    rates: RatesView | None = None
    draft: NoteDraft | None = None
    status: Literal["ok", "failed"] = "ok"
    error: str | None = None
    model: str | None = None
    cost_usd: float | None = None
    #: Numbers in the draft not found in the facts, after one regeneration.
    unverified_numbers: list[str] = Field(default_factory=list)
    #: Added after the note is written; stored separately, never part of the draft.
    follow_up: FollowUp | None = None


class NoteSummary(BaseModel):
    """One written note, for lists and links (the full note is ``/desk/notes/{id}``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    kind: NoteKind
    #: When the note was written (UTC).
    at: AwareDatetime
    headline: str
    summary: str
    tone: Tone


class NoteList(BaseModel):
    """Written notes in a half-open UTC window ``[from, to)``, newest first."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    start: AwareDatetime = Field(alias="from")
    end: AwareDatetime = Field(alias="to")
    limit: int
    #: True when more notes in the window were cut off by ``limit``.
    truncated: bool
    notes: list[NoteSummary]


class LearnEntry(BaseModel):
    """One lesson from a note's Learn box; repeats are merged."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    #: The newest note that taught it.
    note_id: str
    kind: NoteKind
    title: str
    created_at: AwareDatetime
    #: How many notes taught (essentially) the same lesson.
    times: int = Field(ge=1)
    #: The lesson's topic, from fixed keyword rules (``core/desk/lessons.py``).
    topic: str | None = None
    topic_label: str | None = None
    #: A short title for the card, e.g. "Bid-to-cover"; null when none stands out.
    heading: str | None = None


class LearnTopic(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    label: str
    count: int = Field(ge=1)


class LearnLibrary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    count: int
    entries: list[LearnEntry]
    #: Topics with at least one lesson, in a fixed order.
    topics: list[LearnTopic] = Field(default_factory=list)
    #: Lessons taught more than once, most often first.
    most_taught: list[LearnEntry] = Field(default_factory=list)
    #: Notes with a Learn box (repeats counted).
    taught: int = 0


class PlaybookRelease(BaseModel):
    """One past release of an indicator and how markets moved after it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    at: AwareDatetime
    #: The release moment's title, e.g. "Jobless claims & Retail sales".
    title: str
    figure: Figure
    #: Other figures out at the same moment; the market reacted to all of them.
    alongside: list[str] = Field(default_factory=list)
    nq_15m: Move | None = None
    us2y_15m: Move | None = None
    nq_close: Move | None = None
    #: True while the close has not happened (or not reached the delayed feed) yet.
    close_pending: bool = False
    #: How NQ's move by the close compared with its first 15 minutes.
    held: FollowUpVerdict | None = None
    #: The desk note written for this release, if there is one.
    note_id: str | None = None
    #: Actual minus forecast at the reporting precision (the plot's x axis).
    surprise: float | None = None
    surprise_text: str | None = None


class PlaybookBucket(BaseModel):
    """An indicator's releases that came in above, below or in line with the forecast."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: Literal["above", "below", "in_line"]
    count: int = Field(ge=1)
    first_up: int
    first_down: int
    first_flat: int
    close_up: int
    close_down: int
    close_flat: int
    #: Releases whose close is not measured (yet, or no quote).
    close_missing: int
    text: str
    #: NQ's average move in the first 15 minutes (percent), null when none measured.
    nq_15m_avg: float | None = None


class PlaybookIndicator(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    count: int = Field(ge=1)
    #: At least the minimum number of releases needed to talk about a pattern.
    enough: bool
    text: str
    buckets: list[PlaybookBucket]
    #: Newest first.
    releases: list[PlaybookRelease]


class Playbook(BaseModel):
    """How NQ reacted to each indicator's surprises. Counting only; no AI."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    generated_at: AwareDatetime
    #: The oldest release with a measured reaction.
    since: AwareDatetime | None
    min_releases: int
    indicators: list[PlaybookIndicator]


class WatchReading(BaseModel):
    """Rule of thumb for a kind of figure: what a beat or a miss usually means."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    family: Literal["inflation", "jobless", "growth", "fed"]
    label: str
    above: str
    below: str


class WatchFigure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    forecast_text: str
    previous_text: str
    #: Set once the figure is out.
    actual_text: str | None = None
    verdict: Verdict | None = None
    #: The playbook's lines for this figure (empty when it has none).
    history: list[str] = Field(default_factory=list)
    history_note: str | None = None


class WatchCard(BaseModel):
    """What to watch in a coming high-importance release. Deterministic, no AI."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    at: AwareDatetime
    title: str
    importance: str
    explainer: str | None = None
    #: True once the release time has passed (the note follows ~26 minutes later).
    released: bool = False
    figures: list[WatchFigure] = Field(default_factory=list)
    readings: list[WatchReading] = Field(default_factory=list)


class BreakingAlert(BaseModel):
    """A headline that matched a breaking-news rule (no AI involved)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    rule: str
    label: str
    headline: str
    source: str
    url: str | None = None
    published_at: AwareDatetime
    created_at: AwareDatetime


class NextEvent(BaseModel):
    """The next high-impact release, for the Today card. Deterministic, no AI."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    at: AwareDatetime
    title: str
    importance: str
    kind: Literal["economic", "fed"]
    explainer: str | None = None
    #: e.g. "Initial jobless claims: forecast 225K".
    forecast_text: str | None = None
    #: How NQ moved after this figure's earlier releases (playbook counts).
    playbook_text: str | None = None


class CalendarFigure(Figure):
    """A calendar row's figure with a plain-English read of its surprise."""

    #: For the economy, from the figure's reading family: lower inflation or
    #: jobless figures, higher growth figures read "better". Null when there is
    #: no actual or forecast, it matched, or the figure has no clear good side.
    reading: Literal["better", "worse"] | None = None
    #: e.g. "Cooler than forecast", "Stronger than forecast".
    reading_text: str | None = None


class CalendarRow(BaseModel):
    """One release moment on the Calendar page. Deterministic, no AI."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    at: AwareDatetime
    title: str
    codes: list[str]
    importance: Literal["critical", "high", "medium", "low"]
    kind: Literal["economic", "fed", "speech"]
    released: bool
    figures: list[CalendarFigure] = Field(default_factory=list)
    #: Where a Fed speech takes place.
    venue: str | None = None
    #: NQ's move in the first 15 minutes (percent), once measured.
    nq_15m: float | None = None
    nq_15m_text: str | None = None
    #: ``upcoming``: not out yet; ``waiting``: the delayed feed has not caught up;
    #: ``measured``; ``unavailable``: no usable quotes around the release.
    nq_15m_status: Literal["upcoming", "waiting", "measured", "unavailable"]
    note_id: str | None = None
    #: The explainer library key for hover text, e.g. ``indicator:CPI``.
    explainer_key: str | None = None
    explainer: str | None = None
    #: How NQ moved after this figure's earlier releases (counts, no AI).
    playbook_text: str | None = None
    playbook: PlaybookIndicator | None = None


class CalendarDay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The day in the requested time zone.
    date: str
    #: e.g. "Thursday, Oct 22".
    label: str
    rows: list[CalendarRow]


class CalendarBoard(BaseModel):
    """The Calendar page: a week of release moments grouped by the reader's day."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    generated_at: AwareDatetime
    week: Literal["this", "next"]
    #: The New York week, Monday 00:00 to the next Monday (half-open), in UTC.
    start: AwareDatetime
    end: AwareDatetime
    #: The IANA time zone the days are grouped in.
    tz: str
    #: False: market-moving (medium importance and up) only.
    all: bool
    #: Low-importance moments left out by the filter.
    hidden: int = Field(ge=0)
    days: list[CalendarDay]
    stale: bool
    #: Source and coverage caveats (shown behind an ⓘ).
    coverage: list[str] = Field(default_factory=list)


class MacroRelease(BaseModel):
    """A Macro card's last release against its forecast (from the calendar)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    at: AwareDatetime
    #: The figure, with the Calendar page's better/worse read for the economy.
    figure: CalendarFigure
    #: e.g. "Last release: above forecast".
    text: str
    #: What a surprise in this direction usually means for NQ (colours only).
    nq_effect: Literal["good", "bad", "mixed", "neutral"]


class MacroCard(BaseModel):
    """One Macro card. Deterministic, no AI."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    group: Literal["inflation", "jobs", "growth", "fed"]
    title: str
    subtitle: str | None = None
    #: Null when unavailable (``value_text`` is then "—").
    value: float | None
    value_text: str
    #: e.g. "Sep 2026", "Q2 2026", or the time of a quote.
    period_text: str | None
    #: For the next Fed decision: when it is (the UI counts down to it).
    at: AwareDatetime | None = None
    trend: Literal["rising", "falling", "steady", "unknown"]
    trend_text: str
    #: What a rise usually means for NQ, and the same in words.
    nq_when_rising: Literal["bad", "good", "mixed"]
    nq_text: str
    #: The trend's usual meaning for NQ: the colour of the arrow and sparkline.
    nq_effect: Literal["good", "bad", "mixed", "neutral"]
    #: Recent readings, oldest first, for the sparkline.
    points: list[float] = Field(default_factory=list)
    release: MacroRelease | None = None
    explainer_key: str | None = None
    #: Source, adjustment, release time and merged duplicates (behind the ⓘ).
    sources: list[str] = Field(default_factory=list)
    stale: bool = False
    #: A market quote card (the 2-year yield) names its symbol for the sparkline.
    quote_symbol: str | None = None


class MacroGroup(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: Literal["inflation", "jobs", "growth", "fed"]
    title: str
    cards: list[MacroCard]


class MacroBoard(BaseModel):
    """The Macro page: the economy at a glance, in four groups."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    generated_at: AwareDatetime
    groups: list[MacroGroup]


NewsCategory = Literal["fed", "economy", "big_tech", "energy", "geopolitics"]


class NewsItem(BaseModel):
    """One headline on the News page, tagged by fixed rules (no AI)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    at: AwareDatetime
    source: str
    source_label: str
    headline: str
    url: str | None = None
    #: Null: not market-moving by the rules (hidden behind a count).
    category: NewsCategory | None = None
    category_label: str | None = None
    #: Published just after a scheduled release or Fed event of its kind.
    scheduled: bool = False
    #: NQ's move in the 15 minutes after publication (percent), market-moving only.
    nq_15m: float | None = None
    nq_15m_text: str | None = None
    #: ``unmeasured``: not market-moving; ``waiting``: the delayed feed has not
    #: caught up; ``measured``; ``unavailable``: no usable quotes.
    nq_15m_status: Literal["unmeasured", "waiting", "measured", "unavailable"]
    #: It also triggered a breaking-news alert.
    alert: bool = False


class NewsBoard(BaseModel):
    """The News page: recent headlines, newest first, and today's breaking alerts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    generated_at: AwareDatetime
    start: AwareDatetime
    items: list[NewsItem]
    #: Headlines with no market-moving category.
    hidden: int = Field(ge=0)
    alerts: list[BreakingAlert] = Field(default_factory=list)


class DeskView(BaseModel):
    """Everything the Today screen needs, in one response."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    generated_at: datetime
    session_text: str
    quotes: list[Quote]
    rates: RatesView | None
    notes: list[DeskNote]
    upcoming: list[UpcomingItem]
    #: Breaking-news alerts from the last 12 hours, newest first.
    alerts: list[BreakingAlert] = Field(default_factory=list)
    #: "What to watch" cards for high-importance releases due soon.
    watch: list[WatchCard] = Field(default_factory=list)
    #: The next high or critical release within two weeks.
    next_event: NextEvent | None = None
    writer_ready: bool
    writer_detail: str | None = None
