import { useState } from "react";
import { usePoll } from "../chart/usePoll";
import { Explain } from "../shell/Explain";
import { fetchLearn, type LearnEntry } from "../today/api";
import { KIND_LABEL } from "../today/Today";

// The Learn page: every lesson from the desk notes' Learn boxes, newest first,
// with topic chips and a progress panel. Topics, headings and counts come from
// the core (core/desk/lessons.py); the page only filters and searches.

const etDate = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", weekday: "short", month: "short", day: "numeric" });

function Lesson({ entry }: { entry: LearnEntry }) {
  const from = `${KIND_LABEL[entry.kind]} note · ${etDate.format(new Date(entry.created_at))}`;
  return <li className="ln-card">
    <header className="ln-head">
      <h2>{entry.heading ?? entry.topic_label ?? "Lesson"}</h2>
      {entry.times > 1 && <span className="ln-times">taught {entry.times} times</span>}
    </header>
    <p>{entry.text}</p>
    <Explain title={entry.title} noteId={entry.note_id}
      lines={[`Taught in the ${KIND_LABEL[entry.kind].toLowerCase()} note "${entry.title}", ${etDate.format(new Date(entry.created_at))}.`,
        ...(entry.topic_label ? [`Topic: ${entry.topic_label}.`] : []),
        ...(entry.times > 1 ? [`${entry.times} notes taught this lesson; this is the newest.`] : [])]}>
      <span className="t-muted">From: {from}</span>
    </Explain>
  </li>;
}

export function LearnPage() {
  const library = usePoll(fetchLearn, "learn", 300_000);
  const [query, setQuery] = useState("");
  const [topic, setTopic] = useState<string | null>(null);
  const data = library.data;
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  const shown = (data?.entries ?? []).filter((entry) => {
    if (topic && entry.topic !== topic) return false;
    const haystack = `${entry.text} ${entry.title} ${entry.heading ?? ""}`.toLowerCase();
    return words.every((word) => haystack.includes(word));
  });
  return <main className="t-page ln-page">
    <div className="ln-main">
      <header className="page-head">
        <h1>Learn</h1>
        <p className="t-muted">Every lesson from the desk's notes, newest first. Repeated lessons appear once.</p>
      </header>
      <input className="t-search" type="search" placeholder="Search lessons, e.g. yields, CPI, Fed" aria-label="Search lessons"
        value={query} onChange={(event) => setQuery(event.target.value)} />
      {data && data.topics.length > 0 && <nav className="news-filters" aria-label="Topics">
        <button aria-pressed={topic === null} onClick={() => setTopic(null)}>All</button>
        {data.topics.map((t) => <button key={t.key} aria-pressed={topic === t.key} onClick={() => setTopic(t.key)}>{t.label}</button>)}
      </nav>}
      {library.error && <div className="t-error" role="alert">Learn library unavailable · {library.error}</div>}
      {!data && !library.error && <p className="t-muted" role="status">Loading lessons…</p>}
      {data && data.count === 0 && <p className="t-muted">No lessons yet. Each note adds one.</p>}
      {data && data.count > 0 && shown.length === 0 && <p className="t-muted">No lesson matches{query ? ` "${query}"` : ""}{topic ? " in this topic" : ""}.</p>}
      <ul className="ln-grid">{shown.map((entry) => <Lesson key={entry.note_id} entry={entry} />)}</ul>
    </div>
    <aside className="t-panel ln-progress" aria-label="Your progress">
      <h2>Your progress</h2>
      {data && <>
        <p>{data.count} lesson{data.count === 1 ? "" : "s"} · {data.topics.length} topic{data.topics.length === 1 ? "" : "s"}</p>
        <ul className="ln-topics">{data.topics.map((t) => <li key={t.key}><span>{t.label}</span><span className="t-muted">{t.count}</span></li>)}</ul>
        <h3>Most taught</h3>
        {data.most_taught.length === 0 ? <p className="t-muted">No lesson has come up twice yet.</p>
          : <ul className="ln-topics">{data.most_taught.map((e) => <li key={e.note_id}><span>{e.heading ?? e.text.slice(0, 40)}</span><span className="t-muted">{e.times}×</span></li>)}</ul>}
      </>}
    </aside>
  </main>;
}
