import { useState } from "react";
import { usePoll } from "../chart/usePoll";
import { explainerLines, useExplainers, type Explainer } from "../explainers/library";
import { Explain, NoteLink } from "../shell/Explain";
import type { PlaybookRelease } from "../today/api";
import { fetchCalendar, type CalendarBoard, type CalendarFigure, type CalendarRow, type Week } from "./api";

// The Calendar page: a New York week of release moments, grouped by day in
// your time zone, your time first and New York second. Every number and word
// in a row comes from the core; the page lays it out and explains it on click.

const browserZone = () => Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";

const clock = (timeZone: string) => new Intl.DateTimeFormat("en-GB", { timeZone, hour: "2-digit", minute: "2-digit", hour12: false });
const NY = clock("America/New_York");

/** "UTC+7" for the zone right now. */
export function zoneOffset(timeZone: string, at = new Date()): string {
  const name = new Intl.DateTimeFormat("en-US", { timeZone, timeZoneName: "shortOffset" }).formatToParts(at)
    .find((part) => part.type === "timeZoneName")?.value ?? "";
  return name.replace(/^GMT/, "UTC") || "UTC";
}

/** "in 5d 14h", "in 2h 05m", "in 12m": time until a release, for display only. */
export function countdown(at: string, now: number): string {
  const minutes = Math.max(0, Math.round((Date.parse(at) - now) / 60_000));
  if (minutes < 60) return `in ${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `in ${hours}h ${String(minutes % 60).padStart(2, "0")}m`;
  return `in ${Math.floor(hours / 24)}d ${hours % 24}h`;
}

const IMPACT: Record<CalendarRow["importance"], [string, number]> = {
  critical: ["Critical", 3], high: ["High", 3], medium: ["Medium", 2], low: ["Low", 1],
};

const IMPACT_LINES = [
  "How much this release usually matters, from the calendar source.",
  "Critical and high releases often move NQ within minutes; medium ones sometimes do; low ones rarely do.",
  "Market-moving only hides the low ones.",
];

const SURPRISE_LINES = [
  "Actual minus forecast, at the precision the figure is reported in.",
  "Better or worse is for the economy: cooler inflation, fewer jobless and stronger growth read better. It is not a call on NQ: good news for the economy can still push NQ down if it means fewer rate cuts.",
];

function rowKey(row: CalendarRow): string {
  return `${row.at}:${row.codes.join("+")}`;
}

function eventLines(row: CalendarRow, entry: Explainer | undefined): string[] {
  return [
    ...(row.venue ? [`Venue: ${row.venue}.`] : []),
    ...(entry ? explainerLines(entry) : row.explainer ? [row.explainer] : []),
    "A rule of thumb, not advice.",
  ];
}

function surpriseLines(figure: CalendarFigure): string[] {
  return [
    `${figure.name}: actual ${figure.actual_text}, forecast ${figure.forecast_text}.`,
    ...(figure.reading_text ? [figure.reading_text + (figure.reading ? ` (${figure.reading} for the economy).` : ".")] : []),
    ...(figure.surprise ? [figure.surprise.text + "."] : []),
    ...SURPRISE_LINES,
  ];
}

/** One figure's surprise chip, or the row's state when nothing is scored. */
function Surprise({ row, figure }: { row: CalendarRow; figure?: CalendarFigure }) {
  if (!row.released) return <span className="cal-chip">upcoming</span>;
  if (!figure || !(figure.surprise || figure.reading_text)) return <span className="t-muted">—</span>;
  return <Explain title={`Surprise · ${figure.name}`} lines={surpriseLines(figure)}>
    <span className={`cal-chip ${figure.reading ?? "neutral"}`}>
      {figure.surprise?.value_text ?? ""} {figure.reading ?? (figure.verdict === "in_line" ? "in line" : "")}
    </span>
  </Explain>;
}

function Value({ text, className = "" }: { text: string | undefined; className?: string }) {
  return !text || text === "—" ? <span className="t-muted">—</span> : <span className={className}>{text}</span>;
}

function NqMove({ row, now }: { row: CalendarRow; now: number }) {
  switch (row.nq_15m_status) {
    case "upcoming": return <span className="t-muted">{countdown(row.at, now)}</span>;
    case "waiting": return <span className="t-muted">measuring…</span>;
    case "unavailable": return <span className="t-muted">not measured</span>;
    default: {
      const value = row.nq_15m ?? 0;
      return <span className={value > 0 ? "up" : value < 0 ? "down" : ""} title={row.nq_15m_text ?? undefined}>{value > 0 ? "+" : ""}{value.toFixed(2)}%</span>;
    }
  }
}

/** One line per figure, like a classic economic calendar; time, impact, NQ and note span them. */
function Row({ row, timeZone, entry, now, selected, onSelect }: {
  row: CalendarRow; timeZone: string; entry?: Explainer; now: number; selected: boolean; onSelect: () => void;
}) {
  const [label, bars] = IMPACT[row.importance];
  const at = new Date(row.at);
  const lines: (CalendarFigure | undefined)[] = row.figures.length > 0 ? row.figures : [undefined];
  const span = lines.length;
  return <>{lines.map((figure, i) => <tr key={figure?.name ?? row.title}
    className={`cal-row imp-${row.importance}${i > 0 ? " cal-sub" : ""}${selected ? " selected" : ""}`} onClick={onSelect}>
    {i === 0 && <>
      <td className="cal-time" rowSpan={span}><b>{clock(timeZone).format(at)}</b><span className="t-muted">{NY.format(at)}</span></td>
      <td className="cal-impact" rowSpan={span}>
        <span className="cal-bars" aria-hidden="true">{[0, 1, 2].map((j) => <i key={j} className={j < bars ? "on" : ""} />)}</span>{label}
      </td>
    </>}
    <td className="cal-event">
      <Explain title={figure?.name ?? row.title} lines={eventLines(row, entry)} noteId={row.note_id}>{figure?.name ?? row.title}</Explain>
    </td>
    <td className="cal-actual">
      <Value text={figure?.actual_text} className={figure?.reading === "better" ? "up" : figure?.reading === "worse" ? "down" : ""} />
    </td>
    <td><Value text={figure?.forecast_text} /></td>
    <td>
      <Value text={figure?.previous_text} />
      {figure?.revised_previous_text && <Explain title={`Previous · ${figure.name}`}
        lines={[`First reported ${figure.previous_text}, now ${figure.revised_previous_text}${figure.revision_text ? ` (${figure.revision_text})` : ""}.`]}>
        <span className="cal-revised"> → {figure.revised_previous_text}</span>
      </Explain>}
    </td>
    <td><Surprise row={row} figure={figure} /></td>
    {i === 0 && <>
      <td className="cal-nq" rowSpan={span}><NqMove row={row} now={now} /></td>
      <td className="cal-note" rowSpan={span}>
        {row.note_id ? <NoteLink noteId={row.note_id}>Read note</NoteLink>
          : !row.released && row.kind !== "speech"
            ? <button type="button" className="t-link" onClick={(event) => { event.stopPropagation(); onSelect(); }}>What to watch</button>
            : <span className="t-muted">—</span>}
      </td>
    </>}
  </tr>)}</>;
}

function Bars({ releases }: { releases: PlaybookRelease[] }) {
  const moves = releases.flatMap((r) => r.nq_15m ? [r.nq_15m.change] : []);
  if (moves.length === 0) return <p className="t-muted">No measured moves.</p>;
  const scale = Math.max(...moves.map(Math.abs), 0.01);
  const width = 28;
  const oldestFirst = [...moves].reverse();
  const text = oldestFirst.map((m) => `${m > 0 ? "+" : ""}${m.toFixed(2)}%`).join(", ");
  return <>
    <svg className="cal-bars-chart" width={moves.length * width} viewBox={`0 0 ${moves.length * width} 80`} role="img"
      aria-label={`NQ 15-minute moves, oldest first: ${text}`}>
      <line x1="0" x2={moves.length * width} y1="40" y2="40" className="cal-axis" />
      {oldestFirst.map((move, i) => {
        const h = Math.abs(move) / scale * 36;
        return <rect key={i} x={i * width + 3} width={width - 6} y={move >= 0 ? 40 - h : 40} height={Math.max(h, 1)} className={move >= 0 ? "up" : "down"} />;
      })}
    </svg>
    <p className="t-muted cal-side-foot">Oldest first: {text}</p>
  </>;
}

function SidePanel({ row, entry, onClose }: { row: CalendarRow; entry?: Explainer; onClose: () => void }) {
  const playbook = row.playbook;
  const groups = (["above", "below", "in_line"] as const).map((verdict) => ({
    verdict,
    bucket: playbook?.buckets.find((b) => b.verdict === verdict),
    releases: playbook?.releases.filter((r) => r.figure.verdict === verdict) ?? [],
  })).filter((g) => g.bucket);
  return <aside className="t-panel cal-side" aria-label={`Playbook: ${row.title}`}>
    <header className="cal-side-head">
      <h2>Playbook: {row.title}</h2>
      <button type="button" className="t-link" aria-label="Close the playbook panel" onClick={onClose}>✕</button>
    </header>
    {row.figures.some((f) => f.forecast !== null) && <p>{row.figures.filter((f) => f.forecast !== null).map((f) => `${f.name}: forecast ${f.forecast_text}`).join(" · ")}</p>}
    {entry?.rising && <p><b>{entry.rising_label}:</b> {entry.rising}</p>}
    {entry?.falling && <p><b>{entry.falling_label}:</b> {entry.falling}</p>}
    <h3>NQ in the first 15 minutes after past releases</h3>
    {row.playbook_text ? <p className="t-muted">{row.playbook_text}</p>
      : <p className="t-muted">{row.kind === "economic" ? "No playbook yet for this release." : "Fed events have no forecast to beat, so they are not counted."}</p>}
    {groups.map(({ verdict, bucket, releases }) => <div key={verdict} className="cal-side-group">
      <b>{verdict === "above" ? "Above forecast" : verdict === "below" ? "Below forecast" : "In line"}</b> <span className="t-muted">({bucket!.count} release{bucket!.count === 1 ? "" : "s"})</span>
      <Bars releases={releases} />
    </div>)}
    {playbook && !playbook.enough && <p className="cal-side-warn">Only {playbook.count} release{playbook.count === 1 ? "" : "s"} measured: examples, not a pattern.</p>}
    {row.note_id && <p><NoteLink noteId={row.note_id} /></p>}
    <p className="t-muted cal-side-foot">Counted by Omarkets from delayed quotes. A rule of thumb, not advice.</p>
  </aside>;
}

function Sources({ board }: { board: CalendarBoard }) {
  const lines = [
    ...(board.stale ? ["No calendar source is healthy right now, so times and figures may be out of date."] : []),
    "Market-moving only: medium, high and critical releases.",
    ...board.coverage,
  ];
  return <Explain title="About these figures" lines={lines}><span className={board.stale ? "t-error" : "t-muted"}>{board.stale ? "Calendar stale ⓘ" : "Sources ⓘ"}</span></Explain>;
}

export function CalendarPage({ timeZone = browserZone() }: { timeZone?: string }) {
  const library = useExplainers();
  const [week, setWeek] = useState<Week>("this");
  const [all, setAll] = useState(false);
  // undefined: the default (the next release, not a speech); null: closed.
  const [selected, setSelected] = useState<string | null | undefined>(undefined);
  const board = usePoll((signal) => fetchCalendar(week, all, timeZone, signal), `${week}:${all}:${timeZone}`, 60_000);
  const now = Date.now();
  const rows = board.data?.days.flatMap((d) => d.rows) ?? [];
  const fallback = rows.find((r) => !r.released && r.kind !== "speech") ?? rows.find((r) => r.playbook);
  const open = selected === undefined ? fallback ?? null : rows.find((r) => rowKey(r) === selected) ?? null;
  const entryFor = (row: CalendarRow) => row.explainer_key ? library?.get(row.explainer_key) : undefined;

  return <main className={`t-page cal-page${open ? " with-side" : ""}`}>
    <section className="t-panel cal-main" aria-label="Economic calendar">
      <header className="chart-toolbar">
        <span className="seg-buttons" role="group" aria-label="Week">
          <button aria-pressed={week === "this"} onClick={() => setWeek("this")}>This week</button>
          <button aria-pressed={week === "next"} onClick={() => setWeek("next")}>Next week</button>
        </span>
        <label className="overlay-toggle">
          <input type="checkbox" checked={!all} onChange={(e) => setAll(!e.target.checked)} /> Market-moving only
        </label>
        {board.data && !all && board.data.hidden > 0 && <span className="t-muted">{board.data.hidden} low-importance hidden</span>}
        <span className="cal-zone t-muted">Times in your time zone ({zoneOffset(timeZone)}), New York below</span>
        {board.data && <Sources board={board.data} />}
      </header>
      {board.error && <p className="t-error" role="status">Calendar unavailable · {board.error}{board.data ? " (showing the last good copy)" : ""}</p>}
      {board.loading && !board.data && <p className="t-muted" role="status">Loading…</p>}
      {board.data && rows.length === 0 && <p className="t-empty">No {all ? "" : "market-moving "}releases scheduled {week === "this" ? "this" : "next"} week.</p>}
      {board.data && rows.length > 0 && <table className="cal-table">
        <thead><tr>
          <th>Your time<br /><span className="t-muted">ET (New York)</span></th>
          <th><Explain title="Impact" lines={IMPACT_LINES}>Impact</Explain></th>
          <th>Event</th><th>Actual</th><th>Forecast</th><th>Previous</th>
          <th><Explain title="Surprise" lines={SURPRISE_LINES}>Surprise</Explain></th>
          <th>NQ 15 min</th><th>Note</th>
        </tr></thead>
        {board.data.days.map((day) => <tbody key={day.date}>
          <tr className="cal-day"><th colSpan={9} scope="rowgroup">{day.label}</th></tr>
          {day.rows.map((row) => <Row key={rowKey(row)} row={row} timeZone={timeZone} entry={entryFor(row)} now={now}
            selected={open !== null && rowKey(row) === rowKey(open)} onSelect={() => setSelected(rowKey(row))} />)}
        </tbody>)}
      </table>}
    </section>
    {open && <SidePanel row={open} entry={entryFor(open)} onClose={() => setSelected(null)} />}
  </main>;
}
