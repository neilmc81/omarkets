import { useState } from "react";
import { usePoll } from "../chart/usePoll";
import { Explain, NoteLink } from "../shell/Explain";
import { fetchPlaybook, type PlaybookIndicator, type PlaybookRelease } from "../today/api";

// The Playbook page: how NQ moved after each indicator's past releases, grouped
// by whether they came in above, below or in line with the forecast. Counts,
// averages and surprises come from the core (core/desk/playbook.py).

const VERDICTS = [["above", "Above forecast"], ["in_line", "In line"], ["below", "Below forecast"]] as const;
const VERDICT_SHORT: Record<PlaybookRelease["figure"]["verdict"], string> = {
  above: "above", below: "below", in_line: "in line", not_reported: "", no_forecast: "",
};

const HELD: Record<NonNullable<PlaybookRelease["held"]>, [string, string]> = {
  held: ["held", "NQ ended the day moving the same way as its first 15 minutes."],
  grew: ["grew", "NQ's move by the close was bigger than its first 15 minutes, in the same direction."],
  faded: ["faded", "NQ kept the direction of its first move but gave most of it back by the close."],
  reversed: ["reversed", "NQ ended the day moving the other way from its first 15 minutes."],
  quiet: ["little first move", "The first 15 minutes barely moved NQ, so there was little to hold."],
};

const etDate = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", month: "short", day: "numeric", year: "numeric" });
const etMonth = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", month: "short", day: "numeric" });
const etClock = new Intl.DateTimeFormat("en-GB", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", hour12: false });

const sign = (v: number) => (v > 0 ? "+" : v < 0 ? "−" : "");
const pct = (v: number) => `${sign(v)}${Math.abs(v).toFixed(2)}%`;
const dir = (v: number | null | undefined) => (v == null ? "" : v > 0 ? "up" : v < 0 ? "down" : "");

function MoveCell({ move }: { move: PlaybookRelease["nq_15m"] }) {
  return move ? <span className={dir(move.change)}>{move.text}</span> : <span className="t-muted">—</span>;
}

function releaseLines(r: PlaybookRelease): string[] {
  return [
    `${r.title}, ${etDate.format(new Date(r.at))} at ${etClock.format(new Date(r.at))} ET.`,
    `${r.figure.name}: actual ${r.figure.actual_text}, forecast ${r.figure.forecast_text}${r.surprise_text ? ` (surprise ${r.surprise_text})` : ""}.`,
    ...(r.alongside.length ? [`Out at the same time: ${r.alongside.join(", ")}. NQ reacted to all of them together.`] : []),
    ...(r.nq_15m ? [`NQ ${r.nq_15m.text} in the first 15 minutes.`] : []),
    ...(r.held ? [`By the close: ${HELD[r.held][1]}`] : r.close_pending ? ["The close is not measured yet."] : []),
  ];
}

function Plot({ releases }: { releases: PlaybookRelease[] }) {
  const points = releases.filter((r) => r.surprise !== null && r.nq_15m);
  if (points.length === 0) return <p className="t-muted">No measured releases to plot.</p>;
  const W = 900, H = 250, L = 46, R = 16, T = 12, B = 34;
  const xMax = Math.max(...points.map((r) => Math.abs(r.surprise!)), 1e-9) * 1.15;
  const yMax = Math.max(...points.map((r) => Math.abs(r.nq_15m!.change)), 0.05) * 1.15;
  const x = (v: number) => L + ((v + xMax) / (2 * xMax)) * (W - L - R);
  const y = (v: number) => T + ((yMax - v) / (2 * yMax)) * (H - T - B);
  const yTicks = [-yMax, -yMax / 2, 0, yMax / 2, yMax];
  const unit = points[0].figure.unit;
  return <figure className="pb-plot">
    <figcaption className="t-muted">NQ move in the first 15 min (%) against the surprise (actual minus forecast)</figcaption>
    <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Surprise against NQ's 15-minute move for ${points.length} releases`}>
      {yTicks.map((t) => <g key={t}>
        <line x1={L} x2={W - R} y1={y(t)} y2={y(t)} className={t === 0 ? "pb-zero" : "pb-grid"} />
        <text x={L - 6} y={y(t) + 4} textAnchor="end">{t.toFixed(2)}</text>
      </g>)}
      <line x1={x(0)} x2={x(0)} y1={T} y2={H - B} className="pb-zero" />
      <text x={L} y={H - 8}>below forecast</text>
      <text x={W - R} y={H - 8} textAnchor="end">above forecast</text>
      <text x={x(0)} y={H - 8} textAnchor="middle">0{unit === "percent" ? " pt" : ""}</text>
      {points.map((r) => {
        const move = r.nq_15m!.change;
        return <g key={r.at} className={`pb-dot ${dir(move) || "flat"}`}>
          <circle cx={x(r.surprise!)} cy={y(move)} r={6}>
            <title>{`${etDate.format(new Date(r.at))}: surprise ${r.surprise_text}, NQ ${r.nq_15m!.text}`}</title>
          </circle>
          <text x={x(r.surprise!) + 9} y={y(move) - 7}>{etMonth.format(new Date(r.at))}</text>
        </g>;
      })}
    </svg>
  </figure>;
}

function Detail({ indicator }: { indicator: PlaybookIndicator }) {
  return <section className="t-panel pb-detail" aria-label={indicator.name}>
    <h2>{indicator.name} · {indicator.count} release{indicator.count === 1 ? "" : "s"}</h2>
    <div className="pb-tiles">{VERDICTS.map(([verdict, label]) => {
      const bucket = indicator.buckets.find((b) => b.verdict === verdict);
      return <Explain key={verdict} block className={`pb-tile v-${verdict}${bucket ? "" : " is-empty"}`} title={label}
        lines={bucket ? [bucket.text, "Average of the measured first-15-minute moves. A handful of releases is not a rule."] : ["No release came in this way yet."]}>
        <b>{label}</b>
        <span className="t-muted">{bucket ? `${bucket.count} release${bucket.count === 1 ? "" : "s"}` : "none yet"}</span>
        <span className="pb-avg">NQ 15 min avg <strong className={dir(bucket?.nq_15m_avg)}>{bucket?.nq_15m_avg != null ? pct(bucket.nq_15m_avg) : "—"}</strong></span>
      </Explain>;
    })}</div>
    <Plot releases={indicator.releases} />
    <div className="pb-table-wrap"><table className="pb-table">
      <thead><tr><th>Released</th><th>Actual vs forecast</th><th>NQ 15 min</th><th>2-year 15 min</th><th>NQ by close</th><th>Did it hold?</th><th>Note</th></tr></thead>
      <tbody>{indicator.releases.map((r) => <tr key={r.at}>
        <td><Explain title={indicator.name} lines={releaseLines(r)} noteId={r.note_id}>{etDate.format(new Date(r.at))}</Explain>
          {r.alongside.length > 0 && <span className="t-muted pb-with">with {r.alongside.length} other figure{r.alongside.length > 1 ? "s" : ""}</span>}</td>
        <td><strong>{r.figure.actual_text}</strong> vs {r.figure.forecast_text}{r.surprise_text && <span className={`t-muted`}> ({r.surprise_text}, {VERDICT_SHORT[r.figure.verdict]})</span>}</td>
        <td><MoveCell move={r.nq_15m} /></td>
        <td><MoveCell move={r.us2y_15m} /></td>
        <td>{r.close_pending ? <span className="t-muted">after the close</span> : <MoveCell move={r.nq_close} />}</td>
        <td>{r.held ? <Explain title="Did it hold?" lines={[HELD[r.held][1]]}><span className={`pb-held h-${r.held}`}>{HELD[r.held][0]}</span></Explain> : <span className="t-muted">—</span>}</td>
        <td>{r.note_id ? <NoteLink noteId={r.note_id}>Read note</NoteLink> : <span className="t-muted">—</span>}</td>
      </tr>)}</tbody>
    </table></div>
    {!indicator.enough && <p className="pb-warn" role="note">⚠ {indicator.text}</p>}
    <p className="t-muted">Other news moves markets too. Counted by Omarkets from delayed quotes. Not trading advice.</p>
  </section>;
}

export function PlaybookPage() {
  const book = usePoll(fetchPlaybook, "playbook", 300_000);
  const [chosen, setChosen] = useState<string | null>(null);
  const indicators = book.data?.indicators ?? [];
  const open = indicators.find((i) => i.name === chosen) ?? indicators[0];
  return <main className="t-page pb-page">
    <header className="page-head">
      <h1>Playbook</h1>
      <p className="t-muted">How NQ moved after each release, grouped by whether it came in above, below or in line with the forecast. Counted by Omarkets from stored releases and delayed quotes{book.data?.since ? ` since ${etDate.format(new Date(book.data.since))}` : ""}.</p>
    </header>
    {book.error && <p className="t-error" role="status">Playbook unavailable · {book.error}{book.data ? " (showing the last good copy)" : ""}</p>}
    {book.loading && !book.data && <p className="t-muted" role="status">Counting releases…</p>}
    {book.data && indicators.length === 0 && <p className="t-empty">No measured releases yet. Each release with a forecast adds one.</p>}
    {open && <div className="pb-layout">
      <nav className="t-panel pb-list" aria-label="Indicators">
        <ul>{indicators.map((i) => <li key={i.name}>
          <button aria-pressed={i === open} onClick={() => setChosen(i.name)}>
            <span className="pb-name">{i.name} · {i.count}</span>
            {!i.enough && <span className="pb-few" title={`Fewer than ${book.data!.min_releases} releases: examples, not a pattern`} aria-label="too few releases">⚠</span>}
            <span aria-hidden="true">›</span>
          </button>
        </li>)}</ul>
      </nav>
      <Detail indicator={open} />
    </div>}
  </main>;
}
