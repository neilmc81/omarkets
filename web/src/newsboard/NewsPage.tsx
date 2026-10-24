import { useState } from "react";
import { usePoll } from "../chart/usePoll";
import { Explain } from "../shell/Explain";
import { CATEGORIES, fetchNewsBoard, type NewsCategory, type NewsItem } from "./api";

// The News page: recent headlines from trusted sources, filtered to what can
// move NQ. Categories, "scheduled" and the NQ move all come from the core.

type Filter = "moving" | NewsCategory | "all";

const LABEL: Record<NewsCategory, string> = {
  fed: "Fed & central banks", economy: "Economy", big_tech: "Big tech", energy: "Energy", geopolitics: "Geopolitics",
};

const ET = new Intl.DateTimeFormat("en-GB", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", hour12: false });
const ET_DAY = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", weekday: "short" });

const CATEGORY_LINES = [
  "Sorted by fixed keyword and source rules, not by AI, so some headlines land in the wrong group.",
  "Market-moving shows every headline with a category; the rest are hidden behind a count.",
];

const SCHEDULE_LINES = [
  "Scheduled: published just after a scheduled economic release or Fed event of its kind, so markets were expecting news.",
  "Unscheduled: news nobody had in the diary; surprises like these can move markets more.",
];

const MOVE_LINES = [
  "How much NQ moved in the 15 minutes after the headline was published, from delayed quotes.",
  "Other news moves markets too, so the move may have nothing to do with this headline.",
];

function Move({ item }: { item: NewsItem }) {
  switch (item.nq_15m_status) {
    case "unmeasured": return <span className="t-muted">—</span>;
    case "waiting": return <span className="t-muted">measuring…</span>;
    case "unavailable": return <span className="t-muted">not measured</span>;
    default: {
      const value = item.nq_15m ?? 0;
      return <span className={value > 0 ? "up" : value < 0 ? "down" : ""} title={item.nq_15m_text ?? undefined}>{value > 0 ? "+" : ""}{value.toFixed(2)}%</span>;
    }
  }
}

function when(at: string, now: number): string {
  const day = ET_DAY.format(new Date(at));
  return now - Date.parse(at) > 20 * 3600_000 || day !== ET_DAY.format(new Date(now)) ? `${day} ${ET.format(new Date(at))}` : ET.format(new Date(at));
}

export function NewsPage() {
  const board = usePoll(fetchNewsBoard, "news", 60_000);
  const [filter, setFilter] = useState<Filter>("moving");
  const now = Date.now();
  const items = board.data?.items ?? [];
  const shown = items.filter((i) => filter === "all" || (filter === "moving" ? i.category !== null : i.category === filter));
  const button = (key: Filter, label: string) => <button key={key} aria-pressed={filter === key} onClick={() => setFilter(key)}>{label}</button>;
  return <main className="t-page news-page">
    <div className="news-main">
      <header className="page-head">
        <h1>News</h1>
        <p className="t-muted">Headlines from the last 24 hours, filtered to what can move NQ. Times are New York.</p>
      </header>
      <nav className="news-filters" aria-label="Filter headlines">
        {button("moving", "Market-moving")}
        {CATEGORIES.map((c) => button(c, LABEL[c]))}
        {button("all", "All")}
      </nav>
      {board.error && <p className="t-error" role="status">News unavailable · {board.error}{board.data ? " (showing the last good copy)" : ""}</p>}
      {board.loading && !board.data && <p className="t-muted" role="status">Loading…</p>}
      {board.data && shown.length === 0 && <p className="t-empty">No headlines here in the last 24 hours.</p>}
      {shown.length > 0 && <table className="news-table">
        <thead><tr>
          <th>Time ET</th><th>Source</th><th>Headline</th>
          <th><Explain title="Category" lines={CATEGORY_LINES}>Category</Explain></th>
          <th><Explain title="Scheduled or not" lines={SCHEDULE_LINES}>Schedule</Explain></th>
          <th><Explain title="NQ next 15 min" lines={MOVE_LINES}>NQ next 15 min</Explain></th>
        </tr></thead>
        <tbody>{shown.map((item) => <tr key={item.id} className={item.alert ? "is-alert" : ""}>
          <td className="news-time">{when(item.at, now)}</td>
          <td>{item.source_label}</td>
          <td className="news-headline">{item.url ? <a href={item.url} target="_blank" rel="noreferrer">{item.headline}</a> : item.headline}</td>
          <td>{item.category ? <span className={`news-cat cat-${item.category}`}>{item.category_label}</span> : <span className="t-muted">—</span>}</td>
          <td>{item.category ? item.scheduled ? "Scheduled" : "Unscheduled" : <span className="t-muted">—</span>}</td>
          <td className="news-move"><Move item={item} /></td>
        </tr>)}</tbody>
      </table>}
    </div>
    <aside className="news-side">
      <section className="t-panel" aria-label="Breaking alerts today">
        <h2>Breaking alerts today</h2>
        {board.data && board.data.alerts.length === 0 && <p className="t-muted">None. Alerts are rare on purpose: only trusted sources and a few kinds of event (emergency Fed action, new tariffs or sanctions, attacks, defaults, trading halts).</p>}
        <ul className="news-alerts">{board.data?.alerts.map((a) => <li key={a.id}>
          <span className="t-muted">{ET.format(new Date(a.published_at))} ET · {a.label}</span>
          {a.url ? <a href={a.url} target="_blank" rel="noreferrer">{a.headline}</a> : <span>{a.headline}</span>}
          <span className="t-muted">{a.source}</span>
        </li>)}</ul>
      </section>
      {board.data && board.data.hidden > 0 && filter !== "all" && <p className="t-panel news-hidden">
        Hidden: {board.data.hidden} headline{board.data.hidden === 1 ? "" : "s"} not related to markets · <button type="button" className="t-link" onClick={() => setFilter("all")}>Show</button>
      </p>}
    </aside>
  </main>;
}
