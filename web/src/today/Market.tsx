import { useContext, useEffect, useState, type MouseEvent } from "react";
import { fetchBars, fetchPins, intervalsFor, type Bars, type ChartRange, type Interval, type KeyLevel, type KeyLevels, type Pin, type RiskTone, type SessionCalendar, type Sparklines } from "../chart/api";
import { etDayClock, levelLines, pinLines, pinTitle, yourTime } from "../chart/explain";
import { SessionChart } from "../chart/SessionChart";
import { Sparkline } from "../chart/Sparkline";
import { usePoll } from "../chart/usePoll";
import { explainerLines, useExplainers, type Explainer } from "../explainers/library";
import { Explain, NavigateContext } from "../shell/Explain";
import type { NextEvent, Quote } from "./api";

// Today's market section: everything here is computed by the core and only
// laid out here (session times, levels, risk tone, bars, countdown target).

const et = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", hour12: false });
const etTime = (iso: string) => et.format(new Date(iso));
const ms = (iso: string) => Date.parse(iso);

export function useNow(intervalMs = 30_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs]);
  return now;
}

/** "in 5d 14h 06m" until a time; "now" once it has passed. */
export function countdown(iso: string, now: number): string {
  const minutes = Math.floor((ms(iso) - now) / 60_000);
  if (minutes <= 0) return "now";
  const d = Math.floor(minutes / 1440);
  const h = Math.floor((minutes % 1440) / 60);
  const m = String(minutes % 60).padStart(2, "0");
  return `in ${d ? `${d}d ` : ""}${d || h ? `${h}h ` : ""}${m}m`;
}

// ---------------------------------------------------------------- timeline

export function SessionTimeline({ calendar, pins, now }: { calendar: SessionCalendar | null; pins: Pin[]; now: number }) {
  if (!calendar) return null;
  const { session } = calendar;
  const start = ms(session.opens_at);
  const span = ms(session.closes_at) - start;
  const at = (iso: string) => Math.min(100, Math.max(0, ((ms(iso) - start) / span) * 100));
  const nowPct = ((now - start) / span) * 100;
  const shown = pins.filter((p) => ms(p.at) >= start && ms(p.at) < start + span);
  const notes = [
    calendar.text,
    ...(session.early_close ? ["Early close today: cash 13:00 ET, futures 13:15 ET."] : []),
    ...(calendar.maintenance_starts_at && calendar.maintenance_ends_at
      ? [`Daily break ${etTime(calendar.maintenance_starts_at)}–${etTime(calendar.maintenance_ends_at)} ET`] : []),
    ...calendar.holidays.map((h) => `${h.early_close ? "" : "Closed "}${new Date(`${h.date}T12:00:00Z`).toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" })}: ${h.name}`),
  ];
  return <section className="timeline" aria-label="Session timeline">
    <div className="timeline-bar">
      {session.segments.map((segment) => { const narrow = at(segment.ends_at) - at(segment.starts_at) < 9; return <div key={segment.name} className={`timeline-seg seg-${segment.name}`}
        style={{ left: `${at(segment.starts_at)}%`, width: `${at(segment.ends_at) - at(segment.starts_at)}%` }}>
        <Explain title={segment.label} lines={[
          `${etDayClock(segment.starts_at)} – ${etDayClock(segment.ends_at)} ET`,
          `${yourTime(segment.starts_at)} – ${yourTime(segment.ends_at)}`,
          ...(segment.explainer ? [segment.explainer] : []),
        ]} className="timeline-label">
          {segment.name === "cash" ? `US cash ${etTime(segment.starts_at)}–${etTime(segment.ends_at)}`
            : segment.name === "asia" ? `Globex open ${etTime(segment.starts_at)}`
            : segment.name === "after_hours" ? (narrow ? "Close" : `Close ${etTime(segment.ends_at)}`)
            : narrow ? segment.label : `${segment.label} ${etTime(segment.starts_at)}`}
        </Explain>
      </div>; })}
      {shown.map((pin) => <span key={pin.at + pin.title} className={`timeline-pin imp-${pin.importance}`} style={{ left: `${at(pin.at)}%` }}>
        <Explain title={pinTitle(pin)} lines={pinLines(pin)} noteId={pin.note_id} className="timeline-pin-dot">
          <span className="visually-hidden">{pin.title}</span>
        </Explain>
      </span>)}
      {nowPct >= 0 && nowPct <= 100 && <span className="timeline-now" style={{ left: `${nowPct}%` }} aria-label="Now"><b>Now</b></span>}
    </div>
    <p className="timeline-notes t-muted">{notes.join(" · ")}</p>
  </section>;
}

// ---------------------------------------------------------------- quote tiles

/** A tile's hover lines; the library entry (when loaded) replaces the short explainer. */
export function quoteLines(q: Quote, asOf: (iso: string) => string, entry?: Explainer): string[] {
  return [
    ...(entry ? explainerLines(entry) : q.explainer ? [q.explainer] : []),
    `Last: ${q.value_text}`,
    `Change since yesterday's close: ${q.change_text ?? "unavailable"}`,
    q.as_of ? asOf(q.as_of) : "Time unavailable",
    ...(q.delayed ? ["Delayed quote: the source sends it late (marked * on the tile)."] : []),
    ...(q.closed && q.closed_text === "Markets closed" ? ["Markets closed: this is the last value from the latest session; it updates again when the market reopens."]
      : q.closed ? ["Cash closed: this is its last value; it updates again from 03:15 ET."]
      : q.stale ? ["Stale: no fresh quote recently. Treat this number as old."] : []),
    "The line is the last 24 hours, in 15-minute steps.",
  ];
}

export function QuoteStrip({ quotes, sparklines, asOf }: { quotes: Quote[]; sparklines: Sparklines | null; asOf: (iso: string) => string }) {
  const library = useExplainers();
  const delayed = quotes.some((q) => q.delayed);
  const lines = new Map(sparklines?.lines.map((line) => [line.symbol, line.points.map((p) => p.value)]) ?? []);
  return <section className="t-strip-wrap" aria-label="Markets">
    <div className="t-strip">
      {quotes.map((q) => <Explain key={q.symbol} block title={q.label} lines={quoteLines(q, asOf, library?.get(`instrument:${q.symbol}`))}
        className={`t-quote ${q.stale && !q.closed ? "is-stale" : ""} ${q.closed ? "is-closed" : ""}`}>
        <span className="t-quote-label">{q.label}{q.delayed ? " *" : ""}</span>
        <span className="t-quote-body">
          <span className="t-quote-nums">
            <strong>{q.value_text}</strong>
            <span className={`t-chg ${q.closed ? "" : q.direction}`}>{q.closed ? q.closed_text ?? "cash closed" : q.stale ? "stale" : q.change_text ?? "—"}</span>
          </span>
          <Sparkline values={lines.get(q.symbol) ?? []} tone={q.closed || q.stale ? "muted" : q.direction} label={`${q.label}, last 24 hours`} />
        </span>
      </Explain>)}
    </div>
    {delayed && <p className="t-footnote">* about 10 minutes delayed. Yields and dollar are live. Changes are since yesterday's close.</p>}
  </section>;
}

// ---------------------------------------------------------------- risk tone

const TONE_CLASS: Record<RiskTone["tone"], string> = {
  risk_on: "up", leaning_risk_on: "up", quiet: "flat", mixed: "warn", leaning_risk_off: "down", risk_off: "down", unclear: "flat", closed: "flat",
};

export function RiskBanner({ tone, error }: { tone: RiskTone | null; error: string | null }) {
  if (!tone) return error ? <p className="risk-banner tone-flat" role="status">Risk tone unavailable: {error}</p> : null;
  const used = tone.inputs.filter((i) => i.used);
  const sentence = (text: string) => text.replace(/[.;]\s*$/, "");
  const lead = tone.tone === "unclear" ? "not enough fresh quotes to judge" : sentence(tone.reasons[0] ?? "");
  return <Explain block title={`Risk tone: ${tone.label}`} lines={[...tone.reasons, tone.rules, "Counted by fixed rules in Omarkets, not by AI. Not advice."]}
    className={`risk-banner tone-${TONE_CLASS[tone.tone]}`}>
    <span className="risk-icon" aria-hidden="true">{tone.tone === "mixed" ? "⚠" : "●"}</span>
    <span><b>Risk tone: {tone.label}</b> — {lead}{tone.tone !== "closed" && used.length > 1 && tone.reasons[1] ? `; ${lowerFirst(sentence(tone.reasons[1]))}` : ""}.</span>
  </Explain>;
}

const lowerFirst = (text: string) => text.startsWith("NQ") || text.startsWith("VIX") ? text : text.charAt(0).toLowerCase() + text.slice(1);

// ---------------------------------------------------------------- levels

const TODAY_LEVELS: KeyLevel["key"][] = ["overnight_high", "overnight_low", "prior_settle", "prior_day_high", "prior_day_low", "gap_vs_settle"];

export function KeyLevelsCard({ levels, error }: { levels: KeyLevels | null; error: string | null }) {
  const library = useExplainers();
  const rows = levels?.levels.filter((l) => TODAY_LEVELS.includes(l.key)) ?? [];
  return <section className="t-panel key-levels" aria-label="Key levels">
    <h2>Key levels</h2>
    {!levels && !error && <p className="t-muted" role="status">Loading levels…</p>}
    {error && <p className="t-muted" role="status">Levels unavailable: {error}</p>}
    <dl>{rows.map((level) => <div key={level.key} className={`level-row level-${level.key}`}>
      <dt><Explain title={level.label} lines={levelLines(level, library?.get(`level:${level.key}`))}>{level.label}{level.partial ? " (so far)" : ""}</Explain></dt>
      <dd className={level.value === null ? "t-muted" : ""}>{level.value_text}</dd>
    </div>)}</dl>
    {levels && <p className="t-muted">{levels.contract ?? "NQ"}{levels.delayed ? " · from delayed quotes" : ""} · hover a level for what it means</p>}
  </section>;
}

// ---------------------------------------------------------------- next event

export function NextEventCard({ event, now }: { event: NextEvent | null | undefined; now: number }) {
  return <section className="t-panel next-event" aria-label="Next high-impact event">
    <h2>Next high-impact event</h2>
    {!event && <p className="t-muted">Nothing high-impact scheduled in the next two weeks.</p>}
    {event && <>
      <Explain title={event.title} lines={[event.explainer ?? "A scheduled release that often moves NQ.", ...(event.forecast_text ? [event.forecast_text] : [])]}>
        <span className="next-title">{event.title}</span>
      </Explain>
      <p className="next-when">{etDayClock(event.at)} ET · {yourTime(event.at)}</p>
      <p className={`next-countdown imp-${event.importance}`}>{countdown(event.at, now)}</p>
      {event.forecast_text && <p className="t-muted">{event.forecast_text}</p>}
      {event.playbook_text && <p className="next-playbook"><b>Playbook:</b> {event.playbook_text}</p>}
    </>}
  </section>;
}

// ---------------------------------------------------------------- chart card

const CHART_LEVELS: KeyLevel["key"][] = ["overnight_high", "overnight_low", "prior_settle"];
const INTERVAL_LABEL: Record<Interval, string> = { "5m": "5-min", "15m": "15-min", "1h": "hourly" };

export function TodayChart({ levels }: { levels: KeyLevels | null }) {
  const [range, setRange] = useState<Extract<ChartRange, "1d" | "5d">>("1d");
  const [interval, setBarSize] = useState<Interval>("5m");
  const [status, setStatus] = useState<string | null>(null);
  const bars = usePoll((signal) => fetchBars("NQ", interval, range, signal), `NQ:${interval}:${range}`, 60_000);
  const pins = usePoll((signal) => fetchPins(range, signal), range, 300_000);
  const navigate = useContext(NavigateContext);
  const openChart = (event: MouseEvent) => {
    if (!navigate || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey) return;
    event.preventDefault();
    navigate("?view=chart");
  };
  const lines = levels?.levels.filter((l) => CHART_LEVELS.includes(l.key)) ?? [];
  return <section className="t-panel today-chart" aria-label="NQ session chart">
    <header className="chart-head">
      <h2>NQ · {range === "1d" ? "this session" : "5 sessions"} · {INTERVAL_LABEL[interval]}</h2>
      <span className="chart-controls">
        <select aria-label="Bar size" value={interval} onChange={(e) => setBarSize(e.target.value as Interval)}>
          {intervalsFor(range).map((i) => <option key={i} value={i}>{i}</option>)}
        </select>
        <span className="seg-buttons" role="group" aria-label="Range">
          {(["1d", "5d"] as const).map((r) => <button key={r} aria-pressed={range === r} onClick={() => setRange(r)}>{r.toUpperCase()}</button>)}
        </span>
        <a href="?view=chart" onClick={openChart} className="t-link">Full chart →</a>
      </span>
    </header>
    <ChartState bars={bars} />
    <SessionChart bars={bars.data} levels={lines} pins={pins.data?.pins ?? []} height={340} onStatus={setStatus}
      label={`NQ candles, ${range === "1d" ? "this session" : "five sessions"}, ${INTERVAL_LABEL[interval]} bars, times in New York.`} />
    <ChartFooter bars={bars.data} status={status} />
  </section>;
}

export function ChartState({ bars }: { bars: { data: Bars | null; error: string | null; loading: boolean } }) {
  if (bars.error) return <p className="t-error" role="status">Chart data unavailable: {bars.error}{bars.data ? " · showing last loaded bars" : ""}</p>;
  if (bars.loading && !bars.data) return <p className="t-muted" role="status">Loading bars…</p>;
  const data = bars.data;
  if (data && data.market_closed) return <p className="t-muted" role="status">Markets closed · showing the last session.</p>;
  if (data && data.status !== "live") return <p className="t-error" role="status">
    {data.status === "stale" ? "Stale: no fresh NQ quote recently; the chart ends at the last stored quote." : "NQ quotes unavailable right now; the chart shows stored quotes only."}
  </p>;
  return null;
}

export function ChartFooter({ bars, status }: { bars: Bars | null; status: string | null }) {
  const through = bars?.last_at ? `Data through ${etDayClock(bars.last_at)} ET` : "No quotes yet";
  return <footer className="chart-foot">
    <span>{status ?? through}</span>
    {bars?.delayed && <span>Delayed about 10 minutes</span>}
    {bars && bars.sources.length > 0 && <span>Source: {bars.sources.join(", ")}{bars.contracts.length ? ` · ${bars.contracts.join(", ")}` : ""}</span>}
    {bars?.bars.some((b) => !b.count) && <span>Gaps: bars with no stored quotes are left empty</span>}
    {bars?.note && <span>{bars.note}</span>}
    <a href="https://www.tradingview.com/" target="_blank" rel="noreferrer">Charts by TradingView</a>
  </footer>;
}
