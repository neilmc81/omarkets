import { usePoll } from "../chart/usePoll";
import { fetchScorecard, type ScorecardPeriod } from "../today/api";
import { KIND_LABEL } from "../today/Today";

// The Scorecard page: what the core's GET /desk/scorecard returns, as tables, and
// nothing else. Counts are split by note kind and prompt version, cost by model
// and prompt version; the page adds and averages nothing.

const day = new Intl.DateTimeFormat("en-US", { timeZone: "UTC", month: "short", day: "numeric" });
const usd = (value: number | null) => (value === null ? "not available" : `$${value.toFixed(4)}`);

function Period({ title, period }: { title: string; period: ScorecardPeriod }) {
  return <section className="t-panel sc-period" aria-label={title}>
    <h2>{title}</h2>
    <p className="t-muted">{day.format(new Date(period.start))} – {day.format(new Date(period.end))} (UTC) · {period.written} written, {period.failed} failed</p>
    {period.groups.length === 0 ? <p className="t-muted">No notes in this period.</p> : <>
      <table className="sc-table">
        <caption>By note kind and prompt version</caption>
        <thead><tr>
          <th scope="col">Kind</th><th scope="col">Prompt</th><th scope="col">Written</th><th scope="col">Failed</th>
          <th scope="col">Useful</th><th scope="col">Not useful</th><th scope="col">Unrated</th>
          <th scope="col">Why: reported / likely / unknown / not recorded</th>
          <th scope="col">Did it hold: grew / held / faded / reversed / little reaction / no verdict</th>
        </tr></thead>
        <tbody>{period.groups.map((g) => <tr key={`${g.kind}:${g.prompt_version}`}>
          <th scope="row">{KIND_LABEL[g.kind]}</th>
          <td>{g.prompt_version}</td><td>{g.written}</td><td>{g.failed}</td>
          <td>{g.ratings.useful}</td><td>{g.ratings.not_useful}</td><td>{g.ratings.unrated}</td>
          <td>{g.why ? `${g.why.reported} / ${g.why.likely} / ${g.why.unknown} / ${g.why.not_recorded}` : "—"}</td>
          <td>{g.verdicts ? `${g.verdicts.grew} / ${g.verdicts.held} / ${g.verdicts.faded} / ${g.verdicts.reversed} / ${g.verdicts.quiet} / ${g.verdicts.no_verdict}` : "—"}</td>
        </tr>)}</tbody>
      </table>
      <table className="sc-table">
        <caption>Stored cost by model and prompt version</caption>
        <thead><tr><th scope="col">Model</th><th scope="col">Prompt</th><th scope="col">Notes</th><th scope="col">Total</th><th scope="col">Mean</th></tr></thead>
        <tbody>{period.costs.map((c) => <tr key={`${c.model}:${c.prompt_version}`}>
          <th scope="row">{c.model}</th><td>{c.prompt_version}</td>
          <td>{c.notes}{c.unpriced > 0 ? ` (${c.unpriced} without a cost)` : ""}</td><td>{usd(c.total_usd)}</td><td>{usd(c.mean_usd)}</td>
        </tr>)}</tbody>
      </table>
    </>}
  </section>;
}

export function ScorecardPage() {
  const card = usePoll(fetchScorecard, "scorecard", 300_000);
  const data = card.data;
  return <main className="t-page sc-page">
    <header className="page-head">
      <h1>Scorecard</h1>
      <p className="t-muted">How the desk's notes are doing: the last 7 days beside the 7 days before. Counts only, split by prompt version.</p>
    </header>
    {card.error && <div className="t-error" role="alert">Scorecard unavailable · {card.error}</div>}
    {!data && !card.error && <p className="t-muted" role="status">Loading the scorecard…</p>}
    {data && <div className="sc-periods">
      <Period title="Last 7 days" period={data.current} />
      {data.previous && <Period title="The 7 days before" period={data.previous} />}
    </div>}
  </main>;
}
