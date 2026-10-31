"""The desk scheduler: decides which notes are due, gathers facts, writes, notifies.

One loop, one minute apart. Each note has a deterministic id, so a restart or a
second tick never produces a duplicate note or notification.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from core.analysis.levels import key_levels
from core.analysis.move_size import MoveSize, move_size
from core.desk import auctions as A
from core.desk import breaking as B
from core.desk import calendar as CAL
from core.desk import facts as F
from core.desk import followup
from core.desk import headlines as HL
from core.desk import identity as ID
from core.desk import macro as M
from core.desk import pins as PN
from core.desk import plan as PL
from core.desk import playbook as P
from core.desk import review as R
from core.desk import watch as W
from core.desk.identity import NQ, PRODUCTION_INSTRUMENTS, Instrument
from core.desk.links import ReleaseNotes
from core.desk.models import (
    BreakingAlert,
    CalendarRow,
    DeskNote,
    DeskView,
    Figure,
    Headline,
    MacroBoard,
    Move,
    NewsBoard,
    NextEvent,
    NoteKind,
    Playbook,
    UpcomingItem,
    WatchCard,
)
from core.desk.prompts import PROMPT_VERSION, has_profile
from core.desk.store import DeskStore
from core.desk.writer import DeskWriter
from core.domain.chart import ChartPin
from core.domain.market_calendar import is_trading_day
from core.domain.sessions import SessionPhase, active_session, next_session_open, session_phase
from core.providers.earnings_nasdaq import EarningsDate, NasdaqEarningsCalendar
from core.providers.treasury_auctions import Auction, TreasuryAuctions

logger = logging.getLogger(__name__)

NEW_YORK = ZoneInfo("America/New_York")
UTC_ZONE = ZoneInfo("UTC")

RELEASE_WINDOW = timedelta(minutes=15)  # reaction measured after each release
FEED_DELAY = timedelta(minutes=10)  # free NQ/ES feed is about ten minutes behind
#: Write once the full 15-minute reaction has arrived through the delayed feed.
RELEASE_DELAY = RELEASE_WINDOW + FEED_DELAY + timedelta(minutes=1)
#: Pre-market runs at 09:00 ET: after the 08:30 data, 30 minutes before the cash open.
PREMARKET_LEAD = timedelta(minutes=30)
RELEASE_WAIT_MAX = timedelta(minutes=45)  # write without a missing actual after this
PRESS_CONFERENCE_WINDOW = timedelta(minutes=60)
#: A "what to watch" card stays at most this long after its release if no note comes.
WATCH_AFTER = timedelta(minutes=50)
#: The playbook behind the cards is recounted at most this often.
PLAYBOOK_TTL = timedelta(minutes=30)
ASK_REUSE = timedelta(minutes=15)  # "Explain the market now" reuses a note this recent
PRESS_CONFERENCE_DELAY = PRESS_CONFERENCE_WINDOW + FEED_DELAY + timedelta(minutes=1)
LOOKBACK = timedelta(hours=6)  # never write about something older than this
MAX_ATTEMPTS = 3
RETRY_AFTER = timedelta(minutes=5)

#: Sources that are about markets, policy or the economy. Other (general world
#: news) sources only contribute headlines that match :data:`_WORLD_KEYWORDS`.
MARKET_SOURCES = frozenset({
    "cnbc_top", "cnbc_markets", "cnbc_economy", "cnbc_tech", "cnbc_earnings",
    "fed_press_monetary", "fed_press_all", "fed_speeches", "treasury_press", "ecb_press",
    "boe_news", "boj_whatsnew", "whitehouse_actions", "federal_register_presidential",
    "ofac_actions", "eia_today", "energy_press",
})
_WORLD_KEYWORDS = re.compile(
    r"\b(war|missile|strike|attack|invasion|sanction|tariff|trade deal|china|taiwan|iran|"
    r"israel|russia|ukraine|opec|oil|ceasefire|nuclear|election|shutdown|default|fed|"
    r"inflation|interest rate|recession|bank|semiconductor|chip|ai)\b",
    re.IGNORECASE,
)

_AUCTION_WORDS = re.compile(r"\b(auction|treasur(y|ies)|bonds?|notes?|yields?|TIPS)\b",
                            re.IGNORECASE)

#: Headlines given to the writer per list, after ranking.
HEADLINE_CAP = 12
#: A scheduled event this long before a large move's window still counts as in it.
MOVE_EVENT_LEAD = timedelta(minutes=15)

Notify = Callable[[str, str], Awaitable[None]]


@dataclass(frozen=True)
class NewsLine:
    """A headline for the writer: whether its time is a real publication time
    (else when Omarkets received it) and whether it has a market category."""

    headline: Headline
    published: bool
    categorised: bool
Job = tuple[str, NoteKind, Callable[[], Awaitable[DeskNote]]]


def et(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return moment.astimezone(NEW_YORK).strftime("%a %b %d, %H:%M ET")


@dataclass(frozen=True)
class DeskSources:
    """Read-only seams into the existing core services."""

    market_state: Callable[[], Any]
    history: Callable[..., Sequence[Any]]
    calendar_events: Callable[[datetime, datetime], Sequence[Any]]
    news_events: Callable[[], Sequence[Any]]
    #: Hourly OHLC buckets (``MarketStateService.ohlc``), for NQ's key levels.
    ohlc: Callable[..., Sequence[Any]] | None = None
    #: The economic service's macro series, for the macro trends in notes.
    macro_series: Callable[[], Sequence[Any]] | None = None


class DeskService:
    def __init__(
        self,
        *,
        sources: DeskSources,
        store: DeskStore,
        writer: DeskWriter,
        earnings: NasdaqEarningsCalendar | None = None,
        auctions: TreasuryAuctions | None = None,
        notify: Notify | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        move_threshold_pct: float = 0.8,
        ask_daily_limit: int = 5,
        enabled: bool = True,
        poll_seconds: float = 60.0,
        local_tz: ZoneInfo = UTC_ZONE,
        daily_plan: bool = True,
        instruments: Sequence[str] = PRODUCTION_INSTRUMENTS,
    ) -> None:
        self.sources = sources
        self.store = store
        self.writer = writer
        self.earnings = earnings
        self.auctions = auctions
        self.notify = notify
        self.clock = clock
        self.move_threshold_pct = move_threshold_pct
        #: Instruments that get pre-market, move and ask notes (NQ only in production).
        #: Each needs a prompt profile at the live prompt version.
        self.instruments = tuple(Instrument.of(s) for s in instruments)
        missing = [i.symbol for i in self.instruments if not has_profile(i.symbol)]
        if missing:
            raise ValueError(f"no {PROMPT_VERSION} prompt for {', '.join(missing)}; "
                             "an instrument needs a reviewed prompt profile before the "
                             "desk can write for it")
        #: Large-move trigger and whether it was scaled, per (instrument, trade date)
        #: (``_move_trigger``).
        self._trigger: dict[tuple[str, date], tuple[float, bool]] = {}
        self.ask_daily_limit = ask_daily_limit
        self.enabled = enabled
        self.poll_seconds = poll_seconds
        #: The reader's time zone, for the daily plan.
        self.local_tz = local_tz
        self.daily_plan = daily_plan
        self._write_lock = asyncio.Lock()
        self._playbook: Playbook | None = None
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self.enabled and self._task is None:
            self._task = asyncio.create_task(self._run(), name="desk")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.store.close()

    async def _run(self) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep the loop alive
                logger.exception("desk tick failed")
            await asyncio.sleep(self.poll_seconds)

    # ------------------------------------------------------------------ reads

    async def view(self, limit: int = 30) -> DeskView:
        now = self.clock()
        state = self.sources.market_state()
        rates = await asyncio.to_thread(F.rates_view, state, self.sources.history, now=now)
        return DeskView(
            generated_at=now,
            session_text=self.session_text(now),
            quotes=F.quote_strip(state),
            rates=rates,
            notes=self.store.recent(limit),
            upcoming=await self.upcoming(now, days=7),
            alerts=self.store.alerts_since(now - timedelta(hours=12)),
            watch=await self.watch(now),
            next_event=await self.next_event(now),
            writer_ready=self.writer.ready,
            writer_detail=self.writer.detail,
        )

    async def playbook(self) -> Playbook:
        """How NQ reacted to each indicator's past surprises (no AI)."""
        now = self.clock()

        def compute() -> Playbook:
            events = self._events_between(now - P.LOOKBACK, now)
            return P.build(events, self.sources.history, now=now, notes=self.release_notes())

        return await asyncio.to_thread(compute)

    def _events_between(self, start: datetime, end: datetime) -> list[Any]:
        """Calendar rows in ``[start, end)``, read in chunks: a long range would hit
        the calendar's row limit."""
        events: list[Any] = []
        while start < end:
            stop = min(start + timedelta(days=90), end)
            events.extend(self.sources.calendar_events(start, stop))
            start = stop
        return events

    async def calendar(self, start: datetime, end: datetime, *,
                       show_all: bool) -> tuple[list[CalendarRow], int]:
        """The Calendar page's rows in ``[start, end)`` and how many low-importance
        moments the market-moving filter left out (no AI)."""
        now = self.clock()
        playbook = await self._recent_playbook(now)

        def compute() -> tuple[list[CalendarRow], int]:
            events = list(self.sources.calendar_events(start, end))
            released = min(end, now)
            # Each released surprise is sized against the figure's own past ones.
            past = (self._events_between(start - F.SURPRISE_LOOKBACK, released)
                    if start < released else [])
            found = CAL.rows(events, self.sources.history, start=start, end=end, now=now,
                             notes=self.release_notes(), playbook=playbook,
                             surprise_history=past, show_all=show_all)
            every = len(F.release_slots(events, start=start, end=end, min_importance="low"))
            return found, 0 if show_all else every - len(found)

        return await asyncio.to_thread(compute)

    async def macro(self, series: Sequence[Any]) -> MacroBoard:
        """The Macro page's cards from the economic service's series (no AI)."""
        now = self.clock()
        state = self.sources.market_state()

        def compute() -> MacroBoard:
            events = self._events_between(now - M.RELEASE_LOOKBACK, now + M.FED_AHEAD)
            rates = F.rates_view(state, self.sources.history, now=now)
            return M.board(series, events, rates, now=now)

        return await asyncio.to_thread(compute)

    async def news(self, hours: int = 24) -> NewsBoard:
        """The News page: recent headlines tagged by fixed rules, with NQ's move (no AI)."""
        now = self.clock()
        start = now - timedelta(hours=hours)
        alerts = self.store.alerts_since(start)

        def compute() -> NewsBoard:
            events = self.sources.calendar_events(start - HL.SCHEDULED_AFTER, now)
            found = HL.items(self.sources.news_events(), events, self.sources.history,
                             now=now, start=start, alert_ids=[a.id for a in alerts])
            return HL.board(found, alerts, now=now, start=start)

        return await asyncio.to_thread(compute)

    def release_notes(self) -> ReleaseNotes:
        """Written release notes, for linking calendar rows and playbook releases."""
        return ReleaseNotes(self.store.release_note_ids())

    async def watch(self, now: datetime) -> list[WatchCard]:
        """"What to watch" cards for high-importance releases due soon (no AI)."""
        events = self.sources.calendar_events(now - WATCH_AFTER, now + W.AHEAD)
        written = [n.id.removeprefix("release:") for n in self.store.recent(30)
                   if n.id.startswith("release:")]
        found = W.cards(events, None, now=now, until_written=WATCH_AFTER, written=written)
        if not found:
            return []
        return W.cards(events, await self._recent_playbook(now), now=now,
                       until_written=WATCH_AFTER, written=written)

    async def _recent_playbook(self, now: datetime) -> Playbook:
        """The playbook, recounted at most every :data:`PLAYBOOK_TTL`."""
        if self._playbook is None or now - self._playbook.generated_at >= PLAYBOOK_TTL:
            try:
                self._playbook = await self.playbook()
            except Exception:  # noqa: BLE001 - cards still help without history
                logger.exception("playbook for the Today cards failed")
                # Retry after the TTL, not on every poll.
                self._playbook = Playbook(generated_at=now, since=None,
                                          min_releases=P.MIN_RELEASES, indicators=[])
        return self._playbook

    async def next_event(self, now: datetime) -> NextEvent | None:
        """The next high-impact release with its playbook line (no AI)."""
        events = self.sources.calendar_events(now, now + PN.NEXT_EVENT_AHEAD)
        if PN.next_event(events, None, now=now) is None:
            return None
        return PN.next_event(events, await self._recent_playbook(now), now=now)

    def chart_pins(self, start: datetime, end: datetime) -> list[ChartPin]:
        """Release pins for the chart between ``start`` and ``end`` (no AI)."""
        now = self.clock()
        events = self.sources.calendar_events(start, end)
        return PN.pins(events, self.sources.history, start=start, end=end, now=now,
                       notes=self.release_notes())

    @staticmethod
    def session_text(now: datetime) -> str:
        phase = session_phase(now)
        if phase is SessionPhase.RTH:
            close = active_session(now).rth_closes_at
            return f"US market open · closes in {_duration(close - now)}"
        if phase is SessionPhase.OVERNIGHT:
            session = active_session(now)
            if now < session.rth_opens_at:
                until_open = _duration(session.rth_opens_at - now)
                return f"Futures trading overnight · US open in {until_open}"
            return "Futures trading after the US close"
        reopen = next_session_open(now)
        label = "Daily maintenance break" if phase is SessionPhase.MAINTENANCE else "Market closed"
        return f"{label} · futures reopen in {_duration(reopen - now)}"

    async def upcoming(self, now: datetime, *, days: int) -> list[UpcomingItem]:
        events = self.sources.calendar_events(now - timedelta(hours=1), now + timedelta(days=days))
        items = F.economic_upcoming(events, now=now, until=now + timedelta(days=days))
        for row in await self._earnings(now.date(), (now + timedelta(days=days)).date()):
            if row.expected_at < now - timedelta(hours=1):
                continue
            items.append(UpcomingItem(
                kind="earnings",
                title=f"{row.company} earnings",
                at=row.expected_at,
                when_text=row.when_text,
                importance="high",
                detail=(f"EPS forecast ${row.eps_forecast:.2f}"
                        if row.eps_forecast is not None else None),
                explainer=(f"{row.company} is one of the largest Nasdaq 100 companies, so "
                           "its results and outlook can move the whole index."),
            ))
        if self.auctions is not None:
            items.extend(A.upcoming_items(await self.auctions.upcoming(), now=now,
                                          until=now + timedelta(days=days)))
        items.sort(key=lambda i: i.at or now)
        return items

    async def _earnings(self, start: date, end: date) -> list[EarningsDate]:
        if self.earnings is None:
            return []
        return await self.earnings.between(start, end)

    def headlines(self, since: datetime, until: datetime, *, limit: int = HEADLINE_CAP,
                  match: re.Pattern[str] | None = None) -> list[Headline]:
        return [line.headline for line in self.news_lines(since, until, limit=limit,
                                                          match=match)]

    def news_lines(self, since: datetime, until: datetime, *, limit: int = HEADLINE_CAP,
                   match: re.Pattern[str] | None = None) -> list[NewsLine]:
        """Headlines in ``[since, until]``, one per title, ranked before the cap.

        Headlines with a market-moving category (``headlines.categorise``) come
        first, then the rest; within each, newest first, then one with a
        publication time before one placed only by when it was received, then
        source, title and link, so equal times always rank the same way.
        Duplicate titles are ranked the same way and the best one is kept, so
        which copy survives never depends on input order. A headline without a
        publication time is placed by when it was received, and says so.
        """
        ranked: list[tuple[tuple, str, NewsLine]] = []
        for event in self.sources.news_events():
            published = event.published_at or event.received_at
            if published is None or not (since <= published <= until):
                continue
            title = event.headline.strip()
            source = event.source
            if source not in MARKET_SOURCES and not _WORLD_KEYWORDS.search(title):
                continue
            if match is not None and not match.search(title):
                continue
            line = NewsLine(
                Headline(source=source, title=title[:300], published_at=published,
                         url=event.source_url),
                published=event.published_at is not None,
                categorised=HL.categorise(title, source) is not None)
            rank = (not line.categorised, -published.timestamp(), not line.published,
                    source, title, event.source_url or "")
            ranked.append((rank, title.casefold(), line))
        ranked.sort(key=lambda r: r[0])
        seen: set[str] = set()
        result: list[NewsLine] = []
        for _, key, line in ranked:
            if key in seen:
                continue
            seen.add(key)
            result.append(line)
        return result[:limit]

    # ------------------------------------------------------------------ scheduling

    async def tick(self) -> list[DeskNote]:
        now = self.clock()
        produced: list[DeskNote] = []
        for job in await self.due_jobs(now):
            note = await self._produce(*job)
            if note is not None:
                produced.append(note)
        await self.update_follow_ups(now)
        await self.scan_breaking(now)
        await self.send_plan(now)
        return produced

    async def send_plan(self, now: datetime) -> bool:
        """The daily plan notification: once per trading day, at 07:00 ET. No AI."""
        if not self.daily_plan or self.notify is None:
            return False
        day = now.astimezone(NEW_YORK).date()
        start, end = PL.window(day)
        if not is_trading_day(day) or not (start <= now < end):
            return False
        if not self.store.claim_once(f"plan:{day.isoformat()}", now):
            return False
        until = PL.covers_until(day)
        items = [i for i in await self.upcoming(now, days=1) if i.at is not None and i.at <= until]
        premarket_at = datetime.combine(day, time(9, 30), tzinfo=NEW_YORK) - PREMARKET_LEAD
        title, body = PL.plan_text(items, now=now, premarket_at=premarket_at, local=self.local_tz)
        try:
            await self.notify(title, body)
        except Exception:  # noqa: BLE001 - notification is best effort
            logger.warning("daily plan notification failed")
        return True

    async def scan_breaking(self, now: datetime) -> list[BreakingAlert]:
        """Alert on fresh, trusted headlines that match a breaking-news rule. No AI."""
        sent = [B.SentAlert(a.rule, a.headline, a.created_at)
                for a in self.store.alerts_since(now - timedelta(hours=24))]
        fresh = []
        for event in self.sources.news_events():
            published = event.published_at or event.received_at
            if published is None or not (now - B.FRESH <= published <= now + timedelta(minutes=5)):
                continue
            fresh.append((published, event))
        fresh.sort(key=lambda item: item[0])
        new: list[BreakingAlert] = []
        for published, event in fresh:
            if self.store.has_alert(event.id):
                continue
            headline = " ".join(event.headline.split())[:300]
            rule = B.match_rule(headline, event.source)
            if rule is None or B.is_repeat(headline, rule.key, sent, now=now):
                continue
            if not B.rate_allows(sent, now=now):
                logger.info("breaking alert held by the rate limit: %s", headline)
                break
            alert = BreakingAlert(
                id=event.id, rule=rule.key, label=rule.label, headline=headline,
                source=B.SOURCE_NAMES.get(event.source, event.source), url=event.source_url,
                published_at=published, created_at=now,
            )
            if not self.store.save_alert(alert):
                continue
            sent.append(B.SentAlert(alert.rule, alert.headline, alert.created_at))
            new.append(alert)
            logger.warning("breaking alert (%s): %s", rule.key, headline)
            if self.notify is not None:
                try:
                    await self.notify(f"Omarkets alert: {rule.label}",
                                      f"{headline} — {alert.source}")
                except Exception:  # noqa: BLE001 - notification is best effort
                    logger.warning("breaking alert notification failed")
        return new

    async def update_follow_ups(self, now: datetime) -> int:
        """Measure any "did it hold?" checkpoint that is now due. No model call."""
        updated = 0
        for note in self.store.recent(40):
            if note.kind not in followup.FOLLOWUP_KINDS:
                continue
            try:
                result = await asyncio.to_thread(
                    followup.update, note, note.follow_up, self.sources.history,
                    now=now, delayed=self._delayed(),
                )
            except Exception:  # noqa: BLE001 - one bad note must not stop the desk
                logger.exception("follow-up for %s failed", note.id)
                continue
            if result is not None:
                self.store.save_follow_up(note.id, result)
                updated += 1
        return updated

    def _should_try(self, note_id: str) -> bool:
        if self.store.has_ok(note_id) or self.store.attempts(note_id) >= MAX_ATTEMPTS:
            return False
        failed = self.store.get(note_id)
        # Back off after a failed attempt instead of retrying every tick.
        return failed is None or self.clock() - failed.created_at >= RETRY_AFTER

    async def due_jobs(self, now: datetime) -> list[Job]:
        jobs: list[Job] = []
        local = now.astimezone(NEW_YORK)

        # Week-ahead primer: Sunday 17:00 ET until Monday noon ET.
        monday = (local + timedelta(days=(7 - local.weekday()) % 7)).date()
        primer_from = datetime.combine(monday - timedelta(days=1), time(17), tzinfo=NEW_YORK)
        primer_to = datetime.combine(monday, time(12), tzinfo=NEW_YORK)
        if primer_from <= local < primer_to:
            note_id = f"primer:{monday.isoformat()}"
            if self._should_try(note_id):
                jobs.append((note_id, NoteKind.PRIMER,
                             lambda n=note_id: self.build_primer(n, now)))

        # Pre-market: 08:00 ET on trading days, until the open + 1 hour.
        session = active_session(now)
        if is_trading_day(session.trade_date):
            start = session.rth_opens_at - PREMARKET_LEAD
            if start <= now < session.rth_opens_at + timedelta(hours=1):
                for inst in self.instruments:
                    pid = ID.note_id(NoteKind.PREMARKET, session.trade_date.isoformat(), inst)
                    if self._should_try(pid):
                        jobs.append((pid, NoteKind.PREMARKET,
                                     lambda n=pid, i=inst: self.build_premarket(n, now, i)))

        # Economic releases and Fed events.
        events = self.sources.calendar_events(now - LOOKBACK - timedelta(hours=1), now)
        for slot in F.release_slots(events, start=now - LOOKBACK, end=now,
                                    min_importance="medium"):
            # Medium-importance slots need real figures; a bare schedule row says nothing.
            if slot.importance == "medium" and not slot.figures and not slot.is_fed:
                continue
            if slot.speakers_only:  # Fed speeches appear in Coming up only
                continue
            note_id = f"release:{slot.key}"
            delay = (PRESS_CONFERENCE_DELAY if "FOMC_PRESS_CONFERENCE" in slot.codes
                     else RELEASE_DELAY)
            if now < slot.at + delay:
                continue
            if not (slot.is_fed or slot.complete or now >= slot.at + RELEASE_WAIT_MAX):
                continue
            if self._should_try(note_id):
                jobs.append((note_id, NoteKind.FED if slot.is_fed else NoteKind.RELEASE,
                             lambda s=slot, n=note_id: self.build_release(n, s, now)))

        # Mega-cap earnings: once results headlines appear.
        for row in await self._earnings(now.date() - timedelta(days=1), now.date()):
            if not (row.expected_at + timedelta(minutes=30) <= now
                    <= row.expected_at + timedelta(hours=4)):
                continue
            note_id = f"earnings:{row.symbol}:{row.day.isoformat()}"
            if not self._should_try(note_id):
                continue
            if self._earnings_headlines(row, now):
                jobs.append((note_id, NoteKind.EARNINGS,
                             lambda r=row, n=note_id: self.build_earnings(n, r, now)))

        # Weekly review: after the week's last close (Friday, or earlier in a
        # holiday-shortened week).
        week = R.review_due(now)
        if week is not None and self._should_try(week.note_id):
            jobs.append((week.note_id, NoteKind.REVIEW,
                         lambda w=week: self.build_review(w.note_id, w, now)))

        # Treasury auctions with notably strong or weak demand.
        if self.auctions is not None:
            results = await self.auctions.results()
            for auction in results:
                if not (now - LOOKBACK <= auction.at <= now - RELEASE_DELAY):
                    continue
                read = A.judge(auction, results)
                if read is None or read.verdict not in ("strong", "weak"):
                    continue
                note_id = f"auction:{auction.auction_date.isoformat()}:{auction.cusip}"
                if self._should_try(note_id):
                    jobs.append((note_id, NoteKind.AUCTION,
                                 lambda a=auction, r=read, n=note_id:
                                 self.build_auction(n, a, r, now)))

        # Large moves: at most one note per instrument every 3 hours.
        for inst in self.instruments:
            move = await self._large_move(now, inst)
            if move is None:
                continue
            mid = ID.note_id(NoteKind.MOVE, f"{move.end_at.astimezone(NEW_YORK):%Y%m%dT%H}", inst)
            recent_moves = self.store.count_since(
                "move", (now - timedelta(hours=3)).isoformat(), instrument=inst.symbol)
            if recent_moves == 0 and self._should_try(mid):
                jobs.append((mid, NoteKind.MOVE,
                             lambda m=move, n=mid, i=inst: self.build_move(n, m, now, i)))
        return jobs

    async def _large_move(self, now: datetime, inst: Instrument = NQ) -> Move | None:
        if session_phase(now) not in {SessionPhase.RTH, SessionPhase.OVERNIGHT}:
            return None
        # Quotes are delayed; measure the last hour of observed quote time.
        moves = await asyncio.to_thread(
            F.measure_moves, self.sources.history,
            start=now - timedelta(minutes=75), end=now, symbols=(inst.symbol,),
            min_window=timedelta(minutes=30),
        )
        threshold, _ = await asyncio.to_thread(self._move_trigger, now, inst)
        if moves and abs(moves[0].change) >= threshold:
            return moves[0]
        return None

    def _earnings_headlines(self, row: EarningsDate, now: datetime) -> list[NewsLine]:
        names = {row.symbol, row.company.split(" ")[0]}
        pattern = re.compile(
            r"\b(" + "|".join(re.escape(n) for n in names) + r")\b.*\b(earn|result|revenue|"
            r"guidance|forecast|quarter|profit|sales|beat|miss)", re.IGNORECASE,
        )
        return self.news_lines(row.expected_at - timedelta(minutes=30), now, match=pattern)

    # ------------------------------------------------------------------ builders

    def _common(self, now: datetime) -> tuple[dict[str, Any], Any]:
        state = self.sources.market_state()
        rates = F.rates_view(state, self.sources.history, now=now)
        return {
            "now": et(now),
            "session": self.session_text(now),
            "markets_today": F.market_rows(state),
            "breaking_alerts": [
                {"what": a.label, "headline": a.headline, "source": a.source,
                 "published": et(a.published_at)}
                for a in self.store.alerts_since(now - timedelta(hours=6))
            ],
            "bond_market": {
                "yields": [
                    {"name": line.label, "level": line.value_text,
                     "change_today": line.day_text, "change_past_week": line.week_text}
                    for line in rates.lines
                ],
                "curve": rates.curve_text,
            },
        }, rates

    def _macro_trends(self, now: datetime, rates: Any) -> dict[str, Any]:
        """The Macro page's trends as writer facts, if the series are available."""
        if self.sources.macro_series is None:
            return {}
        try:
            series = self.sources.macro_series()
            if not series:
                return {}
            # Past releases only: the next-Fed card is not sent to the writer.
            events = self._events_between(now - M.RELEASE_LOOKBACK, now)
            trends = M.writer_trends(M.board(series, events, rates, now=now))
        except Exception:  # noqa: BLE001 - macro trends are optional context
            logger.exception("macro trends unavailable")
            return {}
        return {"macro_trends": trends} if trends else {}

    def _move_size(self, now: datetime, inst: Instrument = NQ) -> MoveSize | None:
        if self.sources.ohlc is None:
            return None
        try:
            return move_size(inst.symbol, self.sources.ohlc, now=now)
        except Exception:  # noqa: BLE001 - move size is optional context
            logger.exception("%s move size unavailable", inst.symbol)
            return None

    def _move_trigger(self, now: datetime, inst: Instrument = NQ) -> tuple[float, bool]:
        """The one-hour move that triggers an instrument's note, and whether it is
        scaled to its typical daily range (once per instrument and trade date) or
        the configured fixed threshold. A storage failure is retried on the next
        poll, not cached."""
        key = (inst.symbol, active_session(now).trade_date)
        if key in self._trigger:
            return self._trigger[key]
        size = self._move_size(now, inst)
        if size is None:
            return self.move_threshold_pct, False
        # Only the current session is ever read; drop older entries.
        self._trigger = {k: v for k, v in self._trigger.items() if k[1] == key[1]}
        self._trigger[key] = (size.trigger_pct(self.move_threshold_pct),
                              size.typical_range_pct is not None)
        return self._trigger[key]

    def _nq_context(self, now: datetime, inst: Instrument = NQ) -> dict[str, Any]:
        """Where the instrument is against its key levels and how big its moves
        normally are."""
        state = self.sources.market_state()
        trigger, scaled = self._move_trigger(now, inst)
        gauge, benchmark = F.VOL_INDEXES.get(inst.symbol, (None, None))
        normal = F.normal_move(
            self._move_size(now, inst),
            state.instruments.get(gauge) if gauge else None,
            state.instruments.get(benchmark) if benchmark else None,
            trigger_pct=trigger, trigger_scaled=scaled, gauge=gauge or "", benchmark=benchmark)
        return {**self._location(now, inst),
                **({"normal_move_size": normal} if normal else {}),
                **F.participation(state, inst.symbol)}

    def _location(self, now: datetime, inst: Instrument = NQ) -> dict[str, Any]:
        """The instrument against its overnight, prior-day and prior-week levels,
        if measurable."""
        if self.sources.ohlc is None:
            return {}
        nq = self.sources.market_state().instruments.get(inst.symbol)
        quote_at = getattr(nq, "timestamp", None)
        # A quote from before this session (e.g. just after the 18:00 ET reopen)
        # would be compared with the new session's levels and an old settle.
        if quote_at is None or quote_at < active_session(now).opens_at:
            return {}
        try:
            levels = key_levels(inst.symbol, nq, self.sources.ohlc, now=now)
        except Exception:  # noqa: BLE001 - price location is optional context
            logger.exception("%s key levels unavailable", inst.symbol)
            return {}
        location = F.price_location(levels, getattr(nq, "value", None), quote_at)
        if location is None:
            return {}
        return {f"{inst.symbol.lower()}_price_location": {**location, "as_of": et(quote_at)}}

    def _delayed(self) -> frozenset[str]:
        state = self.sources.market_state()
        return frozenset(
            s for s, i in state.instruments.items()
            if getattr(i.feed_status, "value", i.feed_status) == "delayed"
        )

    def _sized(self, slot: F.ReleaseSlot) -> list[Figure]:
        """The slot's figures with each surprise sized against the indicator's history."""
        try:
            history = self.sources.calendar_events(slot.at - F.SURPRISE_LOOKBACK, slot.at)
        except Exception:  # noqa: BLE001 - sizing is optional context
            logger.exception("surprise history unavailable for %s", slot.key)
            return slot.figures
        return F.size_surprises(slot.figures, history, before=slot.at)

    @staticmethod
    def _figure_facts(figures: Sequence[Figure]) -> list[dict[str, Any]]:
        return [
            {"name": f.name, "actual": f.actual_text, "forecast": f.forecast_text,
             "previous": f.previous_text, "versus_forecast": f.verdict,
             **({"previous_revised_to": f.revised_previous_text}
                if f.revised_previous_text else {}),
             **({"revision": f.revision_text} if f.revision_text else {}),
             **({"surprise": f.surprise.value_text, "surprise_size": f.surprise.text}
                if f.surprise else {})}
            for f in figures
        ]

    @staticmethod
    def _moves_facts(moves: list[Move]) -> list[dict[str, Any]]:
        return [
            {"market": m.label, "change": m.text, "from": et(m.start_at), "to": et(m.end_at),
             "delayed_feed": m.delayed}
            for m in moves
        ]

    @staticmethod
    def _headline_facts(lines: list[NewsLine]) -> list[dict[str, Any]]:
        """``time`` is when the source published it; ``received`` (instead) is
        when Omarkets first saw a headline that carries no publication time."""
        return [{("time" if n.published else "received"): et(n.headline.published_at),
                 "source": n.headline.source, "headline": n.headline.title}
                for n in lines]

    @staticmethod
    def _upcoming_facts(items: list[UpcomingItem]) -> list[dict[str, Any]]:
        return [
            {"when": et(i.at) if i.kind != "earnings" else f"{et(i.at)[:10]} {i.when_text}",
             "what": i.title, "importance": i.importance, "detail": i.detail}
            for i in items
        ]

    async def build_primer(self, note_id: str, now: datetime) -> DeskNote:
        common, rates = await asyncio.to_thread(self._common, now)
        upcoming = await self.upcoming(now, days=6)
        week_moves = await asyncio.to_thread(
            F.measure_moves, self.sources.history, start=now - timedelta(days=7), end=now,
            delayed=self._delayed(), min_window=timedelta(days=1),
        )
        facts = {**common, **(await asyncio.to_thread(self._macro_trends, now, rates)),
                 "past_week_moves": self._moves_facts(week_moves),
                 "this_week": self._upcoming_facts(upcoming),
                 "recent_headlines": self._headline_facts(
                     self.news_lines(now - timedelta(days=2), now))}
        return await self._write(note_id, NoteKind.PRIMER, "The week ahead", now, facts,
                                 moves=week_moves, upcoming=upcoming, rates=rates)

    async def build_auction(self, note_id: str, auction: Auction, read: A.AuctionRead,
                            now: datetime) -> DeskNote:
        common, rates = await asyncio.to_thread(self._common, now)
        moves = await asyncio.to_thread(
            F.measure_moves, self.sources.history, start=auction.at,
            end=auction.at + RELEASE_WINDOW, delayed=self._delayed(),
        )
        news = self.news_lines(auction.at - timedelta(minutes=10), now,
                                   match=_AUCTION_WORDS)
        facts = {
            **common,
            "auction": {
                "security": auction.label, "results_at": et(auction.at),
                "demand": read.verdict, "summary": read.text,
                "high_yield": f"{read.high_yield:.3f}%",
                "bid_to_cover": f"{read.bid_to_cover:.2f}",
                "average_bid_to_cover_recent": (f"{read.average_bid_to_cover:.2f}"
                                                if read.average_bid_to_cover else None),
                "recent_auctions_compared": read.history_count,
                "primary_dealer_share": (f"{read.dealer_share_pct:.1f}%"
                                         if read.dealer_share_pct is not None else None),
                "primary_dealer_share_recent_average": (
                    f"{read.average_dealer_share_pct:.1f}%"
                    if read.average_dealer_share_pct is not None else None),
                "size": (f"${auction.offering_usd / 1e9:.0f} billion"
                         if auction.offering_usd else None),
                "rule": "strong/weak = bid-to-cover at least 5% above/below the recent average",
            },
            "reaction_after_results": self._moves_facts(moves),
            "headlines_since_results": self._headline_facts(news),
        }
        title = f"Treasury auction · {auction.label}"
        return await self._write(note_id, NoteKind.AUCTION, title, now, facts,
                                 event_at=auction.at, moves=moves, headlines=_titles(news),
                                 rates=rates)

    async def build_review(self, note_id: str, week: R.ReviewWeek, now: datetime) -> DeskNote:
        common, rates = await asyncio.to_thread(self._common, now)
        moves = await asyncio.to_thread(R.week_moves, self.sources.history, week,
                                        delayed=self._delayed())
        daily = await asyncio.to_thread(R.daily_nq, self.sources.history, week)
        events = R.ranked_events(self.store.recent(100), week)
        upcoming = await self.upcoming(now, days=9)
        facts = {
            **common,
            "week": {"from": f"{week.trading_days[0]:%A %b %d}",
                     "to": f"{week.trading_days[-1]:%A %b %d}",
                     "trading_days": len(week.trading_days),
                     "measured_from": et(week.previous_close), "measured_to": et(week.last_close)},
            "week_moves": self._moves_facts(moves),
            "nq_each_day": daily,
            "events_by_nq_move": events,
            "next_week": self._upcoming_facts(upcoming[:8]),
            **(await asyncio.to_thread(self._macro_trends, now, rates)),
        }
        title = f"Week in review · {week.trading_days[0]:%b %d}–{week.trading_days[-1]:%b %d}"
        return await self._write(note_id, NoteKind.REVIEW, title, now, facts,
                                 event_at=week.last_close, moves=moves, upcoming=upcoming[:8],
                                 rates=rates)

    async def build_premarket(self, note_id: str, now: datetime,
                              inst: Instrument = NQ) -> DeskNote:
        common, rates = await asyncio.to_thread(self._common, now)
        session = active_session(now)
        upcoming = [i for i in await self.upcoming(now, days=2)
                    if i.at and i.at < session.rth_closes_at + timedelta(hours=8)]
        news = self.news_lines(now - timedelta(hours=14), now)
        # Data already released this morning (usually 08:30 ET), with its reaction.
        day_start = datetime.combine(now.astimezone(NEW_YORK).date(), time(0), tzinfo=NEW_YORK)
        released = F.release_slots(
            self.sources.calendar_events(day_start - timedelta(hours=1), now),
            start=day_start.astimezone(UTC), end=now, min_importance="medium",
        )
        released_facts = []
        figures = []
        for slot in released:
            if not slot.figures and not slot.is_fed:
                continue
            moves = await asyncio.to_thread(
                F.measure_moves, self.sources.history, start=slot.at,
                end=min(now, slot.at + RELEASE_WINDOW), delayed=self._delayed(),
            )
            sized = self._sized(slot)
            figures.extend(sized)
            released_facts.append({
                "event": slot.title, "released": et(slot.at),
                "what_it_is": F.explainer(slot.primary_code),
                "figures": self._figure_facts(sized),
                "reaction_after_release": self._moves_facts(moves),
            })
        facts = {**common, **(await asyncio.to_thread(self._nq_context, now, inst)),
                 **(await asyncio.to_thread(self._macro_trends, now, rates)),
                 "released_this_morning": released_facts,
                 "still_scheduled_today": self._upcoming_facts(upcoming),
                 "overnight_headlines": self._headline_facts(news),
                 "us_open": et(session.rth_opens_at)}
        title = f"Pre-market · {session.trade_date:%A %b %d}"
        if inst != NQ:
            title = f"{inst.symbol} pre-market · {session.trade_date:%A %b %d}"
        return await self._write(note_id, NoteKind.PREMARKET, title, now, facts,
                                 figures=figures, headlines=_titles(news), upcoming=upcoming,
                                 rates=rates, instrument=inst)

    async def build_release(self, note_id: str, slot: F.ReleaseSlot, now: datetime) -> DeskNote:
        common, rates = await asyncio.to_thread(self._common, now)
        window = (PRESS_CONFERENCE_WINDOW if "FOMC_PRESS_CONFERENCE" in slot.codes
                  else RELEASE_WINDOW)
        moves = await asyncio.to_thread(
            F.measure_moves, self.sources.history, start=slot.at, end=slot.at + window,
            delayed=self._delayed(),
        )
        news = self.news_lines(slot.at - timedelta(minutes=10), now)
        statement = None
        for row in self.sources.calendar_events(slot.at - timedelta(minutes=1),
                                                slot.at + timedelta(minutes=1)):
            text = getattr(getattr(row, "statement", None), "text", None)
            if text:
                statement = text[:6000]
        kind = NoteKind.FED if slot.is_fed else NoteKind.RELEASE
        figures = self._sized(slot)
        facts = {
            **common,
            **(await asyncio.to_thread(self._nq_context, now)),
            "event": {"name": slot.title, "released": et(slot.at),
                      "what_it_is": F.explainer(slot.primary_code)},
            "figures": self._figure_facts(figures),
            "reaction_after_release": self._moves_facts(moves),
            "headlines_since_release": self._headline_facts(news),
            "next_events": self._upcoming_facts((await self.upcoming(now, days=3))[:5]),
            **(await asyncio.to_thread(self._macro_trends, now, rates)),
        }
        if statement:
            facts["fed_statement_text"] = statement
        return await self._write(note_id, kind, slot.title, now, facts, event_at=slot.at,
                                 figures=figures, moves=moves, headlines=_titles(news),
                                 rates=rates)

    async def build_earnings(self, note_id: str, row: EarningsDate, now: datetime) -> DeskNote:
        common, rates = await asyncio.to_thread(self._common, now)
        moves = await asyncio.to_thread(
            F.measure_moves, self.sources.history, start=row.expected_at,
            end=min(now, row.expected_at + timedelta(minutes=60)), delayed=self._delayed(),
        )
        news = self._earnings_headlines(row, now)
        facts = {
            **common,
            **(await asyncio.to_thread(self._nq_context, now)),
            "company": {"name": row.company, "symbol": row.symbol,
                        "reported": f"{row.day:%a %b %d} {row.when_text}",
                        "eps_forecast": (f"${row.eps_forecast:.2f}"
                                         if row.eps_forecast is not None else "not available"),
                        "fiscal_quarter": row.fiscal_quarter},
            "results_headlines": self._headline_facts(news),
            "nq_reaction": self._moves_facts(moves),
        }
        return await self._write(note_id, NoteKind.EARNINGS, f"{row.company} earnings", now,
                                 facts, event_at=row.expected_at, moves=moves,
                                 headlines=_titles(news), rates=rates)

    async def build_move(self, note_id: str, move: Move, now: datetime,
                         inst: Instrument = NQ) -> DeskNote:
        common, rates = await asyncio.to_thread(self._common, now)
        moves = await asyncio.to_thread(
            F.measure_moves, self.sources.history, start=move.start_at, end=move.end_at,
            delayed=self._delayed(),
        )
        news = self.news_lines(move.start_at - timedelta(minutes=60), now)
        recent = [n for n in self.store.recent(5)
                  if n.created_at >= now - timedelta(hours=3) and n.draft is not None]
        direction = "up" if move.change > 0 else "down"
        facts = {
            **common,
            **(await asyncio.to_thread(self._nq_context, now, inst)),
            "the_move": {"market": inst.label, "change": move.text,
                         "from": et(move.start_at), "to": et(move.end_at)},
            "events_in_move_window": F.events_in_window(
                self.sources.calendar_events(move.start_at - MOVE_EVENT_LEAD - timedelta(hours=1),
                                             move.end_at),
                start=move.start_at, end=move.end_at, lead=MOVE_EVENT_LEAD),
            "other_markets_same_window": self._moves_facts(moves),
            "headlines_around_the_move": self._headline_facts(news),
            "recent_omarkets_notes": [{"title": n.title, "summary": n.draft.summary}
                                      for n in recent],
            "next_events": self._upcoming_facts((await self.upcoming(now, days=2))[:4]),
        }
        title = f"{inst.symbol} {direction} {move.text.lstrip('+-')} in the last hour"
        return await self._write(note_id, NoteKind.MOVE, title, now, facts,
                                 event_at=move.start_at, moves=moves, headlines=_titles(news),
                                 rates=rates, instrument=inst)

    async def ask(self, inst: Instrument = NQ) -> DeskNote:
        """Explain the market right now, on request. Bounded per New York day
        across all instruments; a note for the same instrument from the last 15
        minutes is returned instead of writing a new one.

        Both checks run under the write lock, so two requests at once cannot
        both pass the daily limit. An instrument the desk does not write for is
        refused before anything is stored or sent."""
        if inst not in self.instruments:
            raise ValueError(f"the desk does not write for {inst.symbol}")
        async with self._write_lock:
            now = self.clock()
            # A note from the last 15 minutes is still current; reuse it for free.
            fresh = self.store.latest_ok(
                "ask", (now - ASK_REUSE).astimezone(UTC).isoformat(), instrument=inst.symbol)
            if fresh is not None:
                return fresh
            day_start = datetime.combine(now.astimezone(NEW_YORK).date(), time(0),
                                         tzinfo=NEW_YORK)
            if self.store.count_since("ask", day_start.astimezone(UTC).isoformat()) \
                    >= self.ask_daily_limit:
                raise RuntimeError(
                    f"Daily limit of {self.ask_daily_limit} on-demand notes reached")
            aid = ID.note_id(NoteKind.ASK, f"{now:%Y%m%dT%H%M%S}", inst)
            return await self._produce_locked(aid, NoteKind.ASK, lambda: self._ask(aid, now, inst))

    async def _ask(self, aid: str, now: datetime, inst: Instrument) -> DeskNote:
        common, rates = await asyncio.to_thread(self._common, now)
        moves = await asyncio.to_thread(
            F.measure_moves, self.sources.history, start=now - timedelta(hours=2), end=now,
            delayed=self._delayed(), min_window=timedelta(minutes=30),
        )
        news = self.news_lines(now - timedelta(hours=6), now)
        facts = {**common, **(await asyncio.to_thread(self._nq_context, now, inst)),
                 "last_two_hours": self._moves_facts(moves),
                 "recent_headlines": self._headline_facts(news),
                 "next_events": self._upcoming_facts((await self.upcoming(now, days=2))[:5])}
        return await self._write(aid, NoteKind.ASK, "Market right now", now, facts, moves=moves,
                                 headlines=_titles(news), rates=rates, notify=False,
                                 instrument=inst)

    # ------------------------------------------------------------------ writing

    async def _produce(self, note_id: str, kind: NoteKind,
                       build: Callable[[], Awaitable[DeskNote]]) -> DeskNote | None:
        async with self._write_lock:
            return await self._produce_locked(note_id, kind, build)

    async def _produce_locked(self, note_id: str, kind: NoteKind,
                              build: Callable[[], Awaitable[DeskNote]]) -> DeskNote | None:
        """:meth:`_produce` for a caller already holding the write lock."""
        if self.store.has_ok(note_id):
            return self.store.get(note_id)
        try:
            note = await build()
        except Exception as exc:  # noqa: BLE001 - one bad note must not stop the desk
            logger.exception("desk note %s failed", note_id)
            note = DeskNote(id=note_id, kind=kind, title=note_id,
                            created_at=self.clock(), status="failed",
                            error=f"build failed ({type(exc).__name__})")
            self.store.save(note)
        if note.status == "failed":
            await self._alert_if_given_up(note)
        return note

    async def _alert_if_given_up(self, note: DeskNote) -> None:
        """One desktop alert when a note has failed its last retry."""
        if self.store.attempts(note.id) < MAX_ATTEMPTS or self.notify is None:
            return
        if not self.store.claim_failure_alert(note.id):
            return
        logger.warning("desk note %s failed all %d attempts", note.id, MAX_ATTEMPTS)
        try:
            await self.notify(
                f"Omarkets could not write: {note.title}",
                f"Tried {MAX_ATTEMPTS} times; last error: {note.error or 'unknown'}. "
                "Quotes and the calendar keep updating; press \"Explain the market now\" "
                "for a fresh note.",
            )
        except Exception:  # noqa: BLE001 - notification is best effort
            logger.warning("desk failure notification failed for %s", note.id)

    async def _write(self, note_id: str, kind: NoteKind, title: str, now: datetime,
                     facts: dict[str, Any], *, notify: bool = True,
                     instrument: Instrument | None = None, **parts: Any) -> DeskNote:
        if (kind in ID.INSTRUMENT_KINDS) != (instrument is not None):
            symbol = instrument.symbol if instrument else None
            raise ValueError(f"{kind.value} note {note_id}: instrument {symbol} "
                             "does not match its kind")
        result = await self.writer.write(kind, facts, instrument=instrument)
        note = DeskNote(
            id=note_id, kind=kind, title=title, created_at=self.clock(),
            instrument=instrument.symbol if instrument else None,
            draft=result.draft, status="ok" if result.draft else "failed",
            error=result.error, model=f"{result.model} · {PROMPT_VERSION}",
            cost_usd=result.cost_usd,
            unverified_numbers=list(result.unverified_numbers), **parts,
        )
        envelope = self.writer.envelope(kind, result, instrument=instrument)
        self.store.save(note, facts, envelope)
        if note.status == "ok":
            logger.info("desk note %s written (cost=%s)", note_id, result.cost_usd)
            if notify and self.notify is not None:
                try:
                    await self.notify(note.draft.headline, note.draft.summary)
                except Exception:  # noqa: BLE001 - notification is best effort
                    logger.warning("desk notification failed for %s", note_id)
        else:
            logger.warning("desk note %s failed: %s", note_id, result.error)
        return note


def _titles(news: Sequence[NewsLine]) -> list[Headline]:
    return [n.headline for n in news]


def _duration(delta: timedelta) -> str:
    minutes = max(0, int(delta.total_seconds() // 60))
    hours, minutes = divmod(minutes, 60)
    if hours >= 24:
        return f"{hours // 24}d {hours % 24}h"
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"
