import { fetchSparklines } from "../chart/api";
import { Sparkline } from "../chart/Sparkline";
import { usePoll } from "../chart/usePoll";
import { countdown } from "../calendar/CalendarPage";
import { explainerLines, useExplainers, type Explainer } from "../explainers/library";
import { Explain } from "../shell/Explain";
import { fetchMacro, type MacroCard } from "./api";

// The Macro page: the economy at a glance in four groups. Colours mean "usually
// good or bad for NQ" (from the core), not up or down.

const ARROW: Record<MacroCard["trend"], string> = { rising: "↗", falling: "↘", steady: "→", unknown: "·" };
const TONE = { good: "up", bad: "down", mixed: "flat", neutral: "flat" } as const;

function valueLines(card: MacroCard, entry: Explainer | undefined): string[] {
  return [
    ...(entry ? explainerLines(entry) : []),
    `Trend: ${card.trend_text}.`,
    card.nq_text,
    "A rule of thumb, not advice.",
  ];
}

function releaseLines(card: MacroCard): string[] {
  const release = card.release!;
  const f = release.figure;
  return [
    `${f.name}: actual ${f.actual_text}, forecast ${f.forecast_text}.`,
    ...(f.reading_text ? [f.reading_text + (f.reading ? ` (${f.reading} for the economy).` : ".")] : []),
    `Released ${new Date(release.at).toLocaleString("en-US", { timeZone: "America/New_York", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false })} ET.`,
    "The chip's colour is what a surprise this way usually means for NQ; jobs and growth cut both ways, so they stay plain.",
  ];
}

function Card({ card, entry, spark, now }: { card: MacroCard; entry?: Explainer; spark?: Array<number | null>; now: number }) {
  const points = card.quote_symbol ? spark ?? [] : card.points;
  const tone = TONE[card.nq_effect];
  return <article className={`mac-card${card.stale ? " is-stale" : ""}`} aria-label={card.title}>
    <header className="mac-head">
      <span className="mac-title">
        {card.title}
        {card.subtitle && <span className="t-muted">{card.subtitle}</span>}
      </span>
      <Explain title={`${card.title}: sources`} lines={card.sources}><span className="mac-info" aria-label="Sources">ⓘ</span></Explain>
    </header>
    <div className="mac-body">
      <Explain title={card.title} lines={valueLines(card, entry)}>
        <strong className="mac-value">{card.value_text}</strong>
      </Explain>
      {points.length > 1 && <Sparkline values={points} tone={tone} label={`${card.title}, recent readings`} />}
    </div>
    {card.key === "fed_next"
      ? <p className="mac-trend">{card.at ? countdown(card.at, now) : "not scheduled"}</p>
      : <p className={`mac-trend tone-${tone}`}><span aria-hidden="true">{ARROW[card.trend]}</span> {card.trend === "unknown" ? card.trend_text : card.trend}</p>}
    <footer className="mac-foot">
      {card.release
        ? <Explain title={`${card.title}: last release`} lines={releaseLines(card)}>
          <span className={`cal-chip ${card.release.nq_effect === "good" ? "better" : card.release.nq_effect === "bad" ? "worse" : ""}`}>{card.release.text}</span>
        </Explain>
        : card.key.startsWith("fed") || card.quote_symbol ? <span /> : <span className="t-muted">No forecast on file</span>}
      <span className="t-muted">{card.stale && card.value === null ? "unavailable" : card.period_text}{card.stale && card.value !== null ? " · stale" : ""}</span>
    </footer>
  </article>;
}

export function MacroPage() {
  const library = useExplainers();
  const board = usePoll(fetchMacro, "macro", 300_000);
  const sparklines = usePoll(fetchSparklines, "sparklines", 300_000);
  const now = Date.now();
  const sparkFor = (symbol: string) => sparklines.data?.lines.find((l) => l.symbol === symbol)?.points.map((p) => p.value);
  return <main className="t-page mac-page">
    <header className="page-head">
      <h1>The economy at a glance</h1>
      <p className="t-muted">Latest official figures. Click a number for what it means for NQ; ⓘ for sources. Green and red mean usually good or bad for NQ, not up or down.</p>
    </header>
    {board.error && <p className="t-error" role="status">Macro unavailable · {board.error}{board.data ? " (showing the last good copy)" : ""}</p>}
    {board.loading && !board.data && <p className="t-muted" role="status">Loading…</p>}
    {board.data && <div className="mac-groups">{board.data.groups.map((group) => <section key={group.key} className={`t-panel mac-group mac-${group.key}`} aria-label={group.title}>
      <h2>{group.title}</h2>
      <div className="mac-cards">{group.cards.map((card) => <Card key={card.key} card={card} now={now}
        entry={card.explainer_key ? library?.get(card.explainer_key) : undefined}
        spark={card.quote_symbol ? sparkFor(card.quote_symbol) : undefined} />)}</div>
    </section>)}</div>}
  </main>;
}
