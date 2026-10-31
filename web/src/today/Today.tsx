import { Fragment, useEffect, useRef, useState } from "react";
import { fetchLevels, fetchPins, fetchRiskTone, fetchSession, fetchSparklines } from "../chart/api";
import { usePoll } from "../chart/usePoll";
import { NQ_FALLBACK, type FocusState } from "../shell/focus";
import { useShortcuts } from "../shell/useShortcuts";
import { GROUP_LABEL, groupNotes, newSince, useLastSeen, type DayGroup } from "./notesList";
import { fetchNote, type Desk, type Figure, type Note, type Rates, type Upcoming, type WatchCard } from "./api";
import { KeyLevelsCard, NextEventCard, QuoteStrip, RiskBanner, SessionTimeline, TodayChart, useNow } from "./Market";
import "./today.css";

export const KIND_LABEL: Record<Note["kind"], string> = {
  primer: "Week ahead",
  premarket: "Pre-market",
  release: "Economic data",
  fed: "Federal Reserve",
  earnings: "Earnings",
  move: "Big move",
  ask: "Market now",
  review: "Week in review",
  auction: "Treasury auction",
};

const TONE_LABEL: Record<NonNullable<Note["draft"]>["nq_tone"], string> = {
  supportive: "Supportive for NQ",
  negative: "Negative for NQ",
  mixed: "Mixed for NQ",
  neutral: "Neutral for NQ",
};

const TONE_SHORT: Record<NonNullable<Note["draft"]>["nq_tone"], string> = {
  supportive: "Supportive", negative: "Negative", mixed: "Mixed", neutral: "Neutral",
};

const VERDICT_LABEL: Record<Figure["verdict"], string> = {
  above: "Above forecast",
  below: "Below forecast",
  in_line: "In line",
  not_reported: "Not reported yet",
  no_forecast: "No forecast",
};

const etFormat = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York", weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false,
});
const localFormat = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", hour12: false });
const localDay = new Intl.DateTimeFormat(undefined, { weekday: "short" });

const etClock = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", hour12: false });
const etDate = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", weekday: "short", month: "short", day: "numeric" });

export function etTime(iso: string | null): string {
  return iso ? `${etFormat.format(new Date(iso))} ET` : "—";
}

export function localTime(iso: string | null): string {
  if (!iso) return "";
  const date = new Date(iso);
  return `${localDay.format(date)} ${localFormat.format(date)} your time`;
}

export function relative(iso: string | null, now: number): string {
  if (!iso) return "";
  const minutes = Math.round((new Date(iso).getTime() - now) / 60_000);
  const abs = Math.abs(minutes);
  const text = abs < 60 ? `${abs}m` : abs < 48 * 60 ? `${Math.floor(abs / 60)}h ${abs % 60}m` : `${Math.round(abs / 1440)}d`;
  return minutes >= 0 ? `in ${text}` : `${text} ago`;
}


function Figures({ figures }: { figures: Figure[] }) {
  if (figures.length === 0) return null;
  return <table className="t-figures">
    <thead><tr><th>Release</th><th>Actual</th><th>Forecast</th><th>Previous</th><th /><th>Surprise</th></tr></thead>
    <tbody>{figures.map((f) => <tr key={f.name}>
      <td>{f.name}</td><td><strong>{f.actual_text}</strong></td><td>{f.forecast_text}</td><td>{f.previous_text}</td>
      <td><span className={`t-verdict ${f.verdict}`}>{VERDICT_LABEL[f.verdict]}</span></td>
      <td className={`t-surprise ${f.surprise?.size ?? ""}`}>{f.surprise
        ? <>{f.surprise.size !== "in_line" && <b>{f.surprise.value_text} </b>}<span>{f.surprise.text}</span></>
        : "—"}</td>
    </tr>)}</tbody>
  </table>;
}

function Moves({ note }: { note: Note }) {
  if (note.moves.length === 0) return null;
  const first = note.moves[0];
  return <div className="t-moves">
    {note.moves.map((m) => <span key={m.symbol} className={`t-move ${m.change > 0 ? "up" : m.change < 0 ? "down" : "flat"}`}>{m.label} <b>{m.text}</b></span>)}
    <span className="t-muted">{etTime(first.start_at)} → {etTime(first.end_at).replace(/^\w+ /, "")}</span>
  </div>;
}

function NoteCard({ note, open, onToggle, now, headRef }: { note: Note; open: boolean; onToggle: () => void; now: number; headRef: (node: HTMLButtonElement | null) => void }) {
  const draft = note.draft;
  if (!draft) return null;
  return <article className={`t-note kind-${note.kind} ${open ? "open" : ""}`}>
    <button ref={headRef} className="t-note-head" aria-expanded={open} onClick={onToggle}>
      <span className="t-note-meta">
        <span className="t-kind">{KIND_LABEL[note.kind]}</span>
        <span>{note.title}</span>
        <span className="t-muted">{etTime(note.created_at)} · {relative(note.created_at, now)}</span>
      </span>
      <h2>{draft.headline}</h2>
      <p className="t-summary">{draft.summary}</p>
      {note.follow_up && <p className={`t-followup ${note.follow_up.verdict ?? ""}`} title="Did the first reaction hold? Measured by Omarkets from delayed quotes.">
        <b>Did it hold?</b> {note.follow_up.text}
      </p>}
      <span className={`t-tone ${draft.nq_tone}`}>{TONE_LABEL[draft.nq_tone]}</span>
    </button>
    {open && <div className="t-note-body">
      <Figures figures={note.figures} />
      <Moves note={note} />
      <h3>What it means</h3><p>{draft.what_it_means}</p>
      <h3>How markets reacted</h3><p>{draft.market_reaction}</p>
      {draft.rates_read && <><h3>Bonds &amp; rates</h3><p>{draft.rates_read}</p></>}
      {draft.watch_next.length > 0 && <><h3>Watch next</h3><ul>{draft.watch_next.map((w) => <li key={w}>{w}</li>)}</ul></>}
      <aside className="t-learn"><b>Learn</b> {draft.learn}</aside>
      {note.headlines.length > 0 && <details className="t-sources"><summary>Headlines considered ({note.headlines.length})</summary>
        <ul>{note.headlines.map((h) => <li key={h.title}><span className="t-muted">{etTime(h.published_at)} · {h.source}</span> {h.url ? <a href={h.url} target="_blank" rel="noreferrer">{h.title}</a> : h.title}</li>)}</ul>
      </details>}
      <p className="t-provenance">Numbers are computed by Omarkets; the explanation is written by {note.model ?? "the desk writer"}. Not trading advice.</p>
    </div>}
  </article>;
}

function ComingUp({ items, now }: { items: Upcoming[]; now: number }) {
  const [openKey, setOpenKey] = useState<string | null>(null);
  return <section className="t-panel" aria-label="Coming up">
    <h2>Coming up</h2>
    {items.length === 0 && <p className="t-muted">Nothing major scheduled in the next 7 days.</p>}
    <ol className="t-upcoming">{items.map((item) => {
      const key = `${item.title}-${item.at}`;
      return <li key={key} className={`imp-${item.importance}`}>
        <button onClick={() => setOpenKey(openKey === key ? null : key)} aria-expanded={openKey === key}>
          <span className="t-when">{etTime(item.at)}{item.when_text ? ` · ${item.when_text}` : ""}<small>{localTime(item.at)} · {relative(item.at, now)}</small></span>
          <span className="t-what">{item.title}{item.importance !== "medium" && <i className={`t-dot ${item.importance}`} title={item.importance} />}</span>
        </button>
        {openKey === key && <div className="t-explain">{item.explainer && <p>{item.explainer}</p>}{item.detail && <p className="t-muted">{item.detail}</p>}</div>}
      </li>;
    })}</ol>
  </section>;
}

function RatesPanel({ rates }: { rates: Rates | null }) {
  const [explain, setExplain] = useState(false);
  if (!rates) return null;
  return <section className="t-panel" aria-label="Bonds and rates">
    <h2>Bonds &amp; rates</h2>
    <table className="t-rates">
      <thead><tr><th /><th>Now</th><th>Today</th><th>1 week</th></tr></thead>
      <tbody>{rates.lines.map((line) => <tr key={line.symbol}>
        <td>{line.label}</td><td><strong>{line.value_text}</strong></td>
        <td className={dir(line.day_change_bp)}>{line.day_text}</td><td className={dir(line.week_change_bp)}>{line.week_text}</td>
      </tr>)}</tbody>
    </table>
    <p className="t-muted">Curve: {rates.curve_text}. 1bp = 0.01%.</p>
    <button className="t-link" onClick={() => setExplain(!explain)} aria-expanded={explain}>{explain ? "Hide" : "Why yields matter for NQ"}</button>
    {explain && <p className="t-explain">{rates.explainer}</p>}
  </section>;
}

function dir(value: number | null): string {
  return value === null ? "" : value > 0 ? "up" : value < 0 ? "down" : "flat";
}

function WatchCards({ cards, now }: { cards: WatchCard[]; now: number }) {
  if (cards.length === 0) return null;
  return <>{cards.map((card) => <section key={card.key} className={`t-panel t-watch imp-${card.importance}`} aria-label={`What to watch: ${card.title}`}>
    <p className="t-note-meta">
      <span className="t-kind">What to watch</span>
      <span>{etTime(card.at)} · {localTime(card.at)}</span>
      <span className="t-muted">{card.released ? "out now · the desk's note follows about 26 minutes after the release" : relative(card.at, now)}</span>
    </p>
    <h2>{card.title}</h2>
    {card.explainer && <p>{card.explainer}</p>}
    {card.figures.length > 0 && <table className="t-figures">
      <thead><tr><th>Figure</th><th>Forecast</th><th>Previous</th>{card.released && <th>Actual</th>}<th>Earlier releases (playbook)</th></tr></thead>
      <tbody>{card.figures.map((f) => <tr key={f.name}>
        <td>{f.name}</td><td><strong>{f.forecast_text}</strong></td><td>{f.previous_text}</td>
        {card.released && <td>{f.actual_text ?? "—"}{f.verdict && <> <span className={`t-verdict ${f.verdict}`}>{VERDICT_LABEL[f.verdict]}</span></>}</td>}
        <td className="t-watch-history">{f.history.length === 0 ? <span className="t-muted">No measured releases yet</span>
          : <>{f.history.map((line) => <span key={line}>{line}</span>)}{f.history_note && <span className="t-muted">{f.history_note}</span>}</>}</td>
      </tr>)}</tbody>
    </table>}
    {card.readings.map((r) => <div key={r.family} className="t-watch-reading">
      <h3>{r.label}: what a surprise usually means</h3>
      <ul>
        <li><b>{r.family === "fed" ? "If more hawkish" : "If above forecast"}:</b> {r.above}</li>
        <li><b>{r.family === "fed" ? "If more dovish" : "If below forecast"}:</b> {r.below}</li>
      </ul>
    </div>)}
    <p className="t-provenance">Rules of thumb, not predictions. Markets often react to the surprise against the forecast, and other news can outweigh it. Not trading advice.</p>
  </section>)}</>;
}

function NoteRow({ note, group, isNew, onOpen }: { note: Note; group: DayGroup; isNew: boolean; onOpen: () => void }) {
  const draft = note.draft;
  if (!draft) return null;
  const when = new Date(note.created_at);
  return <button className={`t-row ${isNew ? "is-new" : ""}`} aria-expanded={false} onClick={onOpen}>
    <span className="t-row-time">{group === "earlier" && <>{etDate.format(when)} · </>}{etClock.format(when)} ET</span>
    <span className="t-row-kind">{KIND_LABEL[note.kind]}</span>
    <span className="t-row-headline">{draft.headline}</span>
    <span className={`t-tone ${draft.nq_tone}`} title={TONE_LABEL[draft.nq_tone]}>{TONE_SHORT[draft.nq_tone]}</span>
  </button>;
}

const NEW_DIVIDER = <p className="t-new-divider" role="separator"><span>New since you last looked</span></p>;

// The newest note (or the one asked for) is open; the rest are compact rows,
// grouped by New York day. Today, and any day holding the open note or new
// notes, starts open; the rest fold away behind "Yesterday › 4 notes".
function NotesPanel({ notes, openNote, onOpen, now, seenBefore, headRef }: {
  notes: Note[];
  openNote: string | null;
  onOpen: (id: string) => void;
  now: number;
  seenBefore: number | null;
  headRef: (id: string) => (node: HTMLButtonElement | null) => void;
}) {
  const [folded, setFolded] = useState<Partial<Record<DayGroup, boolean>>>({});
  const groups = groupNotes(notes, now);
  const fresh = newSince(notes, seenBefore);
  const firstNew = groups.flatMap((g) => g.notes).find((n) => fresh.has(n.id))?.id;
  return <section className="t-panel t-notes" aria-label="Notes list">
    <h2>Notes</h2>
    {groups.map(({ key, notes: items }) => {
      const holdsOpen = items.some((n) => n.id === openNote);
      const holdsNew = items.some((n) => fresh.has(n.id));
      const open = holdsOpen || !(folded[key] ?? (key !== "today" && !holdsNew));
      const keepOpen = () => setFolded((current) => ({ ...current, [key]: false }));
      const count = `${items.length} note${items.length === 1 ? "" : "s"}`;
      return <div key={key} className="t-day">
        {!open && items.some((n) => n.id === firstNew) && NEW_DIVIDER}
        {key !== "today" && <button className="t-day-head" aria-expanded={open} disabled={holdsOpen}
          onClick={() => setFolded((current) => ({ ...current, [key]: open }))}>
          <span aria-hidden="true">{open ? "▾" : "▸"}</span> {GROUP_LABEL[key]} <span className="t-muted">› {count}</span>
        </button>}
        {open && <ol className={`t-rows ${key === "earlier" ? "is-earlier" : ""}`}>{items.map((note) => <Fragment key={note.id}>
          {note.id === firstNew && <li>{NEW_DIVIDER}</li>}
          <li>{note.id === openNote
            ? <NoteCard note={note} now={now} open headRef={headRef(note.id)} onToggle={() => { keepOpen(); onOpen(""); }} />
            : <NoteRow note={note} group={key} isNew={fresh.has(note.id)} onOpen={() => { keepOpen(); onOpen(note.id); }} />}</li>
        </Fragment>)}</ol>}
      </div>;
    })}
  </section>;
}

// openId: null opens the newest note; "" means every note is collapsed.
// focus: the global focus strip (shell/focus.ts). The market section (tiles,
// chart, levels) follows it; the notes, rates and coming-up are the desk's.
export function Today({ desk, error, openId, onOpen, focus }: {
  desk: Desk | null;
  error: string | null;
  openId: string | null;
  onOpen: (id: string) => void;
  focus?: FocusState;
}) {
  const now = useNow();
  const seenBefore = useLastSeen();
  // Until the core says otherwise (or if it can't), Today is NQ's with the desk's tiles.
  const strip = focus?.data ?? null;
  const instrument = strip?.focus ?? NQ_FALLBACK;
  const symbol = instrument.symbol;
  // While the first focus read is in flight, nothing NQ-only is fetched or drawn.
  const focusPending = Boolean(focus && !focus.data && !focus.error);
  const quotes = strip ? strip.quotes : desk?.quotes ?? null;
  const tiles = strip ? strip.symbols : null;
  const calendar = usePoll(fetchSession, "session", 60_000);
  // Release pins measure NQ's first 15 minutes, so only instruments that carry
  // them ask, and not before the focus is known (it may not be NQ).
  const pinsOn = instrument.release_pins && !focusPending;
  const todayPins = usePoll((signal) => pinsOn ? fetchPins("1d", signal) : Promise.resolve(null),
    pinsOn ? "1d" : `${symbol}:off`, pinsOn ? 300_000 : 3_600_000);
  const levelsOn = instrument.levels && !focusPending;
  const levels = usePoll((signal) => levelsOn ? fetchLevels(symbol, signal) : Promise.resolve(null),
    levelsOn ? symbol : `${symbol}:off`, levelsOn ? 60_000 : 3_600_000);
  const risk = usePoll(fetchRiskTone, "risk", 30_000);
  const sparklines = usePoll((signal) => fetchSparklines(signal, tiles ?? undefined), `sparklines:${tiles?.join(",") ?? ""}`, 300_000);
  const shownLevels = levels.data?.symbol === symbol ? levels.data : null;
  const heads = useRef(new Map<string, HTMLButtonElement>());
  const jumped = useRef(false);
  // A linked note older than the desk's recent list is fetched on its own.
  const [linked, setLinked] = useState<Note | null>(null);
  const [linkError, setLinkError] = useState<string | null>(null);
  const recent = desk?.notes.filter((n) => n.draft) ?? [];
  const missing = desk && openId && !recent.some((n) => n.id === openId) ? openId : null;
  // Newest first: the order rows are shown in and J/K step through.
  const notes = (linked && !recent.some((n) => n.id === linked.id) ? [...recent, linked] : recent)
    .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));
  const openNote = openId ?? notes[0]?.id ?? null;

  useEffect(() => {
    if (!missing || linked?.id === missing) return;
    const controller = new AbortController();
    setLinkError(null);
    fetchNote(missing, controller.signal).then((found) => {
      if (found.draft) setLinked(found);
      else setLinkError("That note was not written (the desk writer failed).");
    }).catch((err: Error) => {
      if (err.name !== "AbortError") setLinkError(`Could not open that note: ${err.message || "unavailable"}`);
    });
    return () => controller.abort();
  }, [missing, linked]);

  // J moves to the next (older) note, K to the previous (newer) one.
  const step = (by: number) => {
    if (!notes.length) return;
    const index = notes.findIndex((n) => n.id === openNote);
    const next = index < 0 ? 0 : Math.max(0, Math.min(notes.length - 1, index + by));
    jumped.current = true;
    onOpen(notes[next].id);
  };
  useShortcuts({ j: () => step(1), k: () => step(-1) });
  useEffect(() => {
    if (!jumped.current || !openNote) return;
    jumped.current = false;
    const head = heads.current.get(openNote);
    head?.focus();
    head?.scrollIntoView?.({ block: "nearest" });
  }, [openNote]);

  return <>
    {error && <div className="t-error" role="alert">{error}{desk ? " · showing last loaded data" : ""}</div>}
    {desk && !desk.writer_ready && <div className="t-error" role="status">AI writer not available: {desk.writer_detail}</div>}
    {desk && desk.alerts.length > 0 && <section className="t-alerts" aria-label="Breaking news">
      {desk.alerts.map((alert) => <p key={alert.id}>
        <b>{alert.label}</b>{" "}
        {alert.url ? <a href={alert.url} target="_blank" rel="noreferrer">{alert.headline}</a> : alert.headline}
        <span className="t-muted"> · {alert.source} · {etTime(alert.published_at)}</span>
      </p>)}
      <p className="t-muted t-alerts-note">Matched by simple keyword rules on trusted sources; headlines, not confirmed analysis.</p>
    </section>}
    {!desk && !error && <p className="t-muted" role="status">Connecting…</p>}
    {calendar.data ? <SessionTimeline calendar={calendar.data} pins={pinsOn ? todayPins.data?.pins ?? [] : []} now={now} />
      : desk && <p className="t-session">{desk.session_text}</p>}
    {quotes && <QuoteStrip quotes={quotes} sparklines={sparklines.data} asOf={(iso) => `As of ${etTime(iso)} · ${localTime(iso)}`} />}
    <RiskBanner tone={risk.data} error={risk.error} />
    <div className="t-market">
      {focusPending ? <section className="t-panel today-chart" aria-busy="true"><p className="t-muted">Loading the focus…</p></section>
        : <TodayChart key={symbol} instrument={instrument} levels={shownLevels} />}
      <aside className="t-market-side">
        <NextEventCard event={desk?.next_event} now={now} />
        <KeyLevelsCard instrument={instrument} levels={shownLevels} error={levels.error} />
      </aside>
    </div>
    <div className="t-grid">
      <main className="t-feed" aria-label="Notes">
        {desk && <WatchCards cards={desk.watch} now={now} />}
        {desk && notes.length === 0 && <section className="t-panel t-empty">
          <h2>No notes yet</h2>
          <p>Omarkets writes a pre-market note at 09:00 ET (after the 08:30 data), a note about 25 minutes after every economic release (15 minutes of market reaction plus the 10-minute feed delay), after Fed decisions and big-tech earnings, when NQ makes a big move, and a week-in-review after Friday's close. Press "Explain the market now" any time.</p>
        </section>}
        {missing && linkError && <div className="t-error" role="alert">{linkError}</div>}
        {notes.length > 0 && <NotesPanel notes={notes} openNote={openNote} onOpen={onOpen} now={now} seenBefore={seenBefore}
          headRef={(id) => (node) => { if (node) heads.current.set(id, node); else heads.current.delete(id); }} />}
      </main>
      <aside className="t-side">
        {desk && <ComingUp items={desk.upcoming} now={now} />}
        {desk && <RatesPanel rates={desk.rates} />}
      </aside>
    </div>
  </>;
}
