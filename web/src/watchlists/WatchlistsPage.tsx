import { useEffect, useMemo, useState, type FormEvent } from "react";
import { fetchChartInstruments, fetchWatchlist, type ChartInstrument, type Watchlist } from "../chart/api";
import { usePoll } from "../chart/usePoll";
import { explainerLines, useExplainers, type Explainer } from "../explainers/library";
import { Explain } from "../shell/Explain";
import { fetchQuotes, saveWatchlist, type QuoteRow } from "./api";
import "./watchlists.css";

// The Watchlists page: rename the one watchlist and add, remove or reorder its
// instruments, then save it with one PUT /api/v1/watchlist. The core checks
// every symbol against the instrument registry (a bad one is a 422, shown
// here); it writes no note and calls no AI. Quotes are the ones the core
// already keeps (GET /api/v1/instruments), read only.

const QUOTES_POLL_MS = 15_000;
const MAX_SYMBOLS = 50;
const MAX_NAME = 60;

const ET = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", hour12: false });
const etTime = (iso: string) => `${ET.format(new Date(iso))} ET`;

type Badge = "live" | "delayed" | "stale" | "closed" | "none";

const BADGE_TEXT: Record<Badge, string> = {
  live: "Live", delayed: "Delayed", stale: "Stale", closed: "Closed", none: "No data",
};

const BADGE_LINE: Record<Badge, string> = {
  live: "Live: the source sends this quote in real time.",
  delayed: "Delayed: the source sends this quote late (about 10 minutes for futures).",
  stale: "Stale: no fresh quote recently. Treat this number as old.",
  closed: "Market closed: this is the last value from the latest session; it updates again when the market reopens.",
  none: "No data: the core has no quote for this instrument yet.",
};

export function badgeOf(row: QuoteRow | undefined): Badge {
  if (!row || row.state.status === "unavailable" || row.state.value === null) return "none";
  if (row.state.market_closed) return "closed";
  if (row.state.status === "stale") return "stale";
  return row.state.feed_status === "delayed" || row.state.expected_delay_seconds > 0 ? "delayed" : "live";
}

const fixed = (value: number | null, digits: number) => value === null ? "—" : value.toFixed(digits);
const signed = (value: number | null, digits: number, suffix = "") =>
  value === null ? "—" : `${value > 0 ? "+" : ""}${value.toFixed(digits)}${suffix}`;
const direction = (value: number | null) => value === null || value === 0 ? "" : value > 0 ? "up" : "down";

function rowLines(symbol: string, inst: ChartInstrument | undefined, row: QuoteRow | undefined, entry?: Explainer): string[] {
  const digits = inst?.price_precision ?? 2;
  const badge = badgeOf(row);
  return [
    ...(entry ? explainerLines(entry) : []),
    ...(inst ? [] : [`${symbol} is not in the instrument registry; the core will refuse to save it.`]),
    ...(row && badge !== "none" ? [
      `Last: ${fixed(row.state.value, digits)}${inst?.unit ? ` ${inst.unit}` : ""}`,
      `Change since the previous close: ${signed(row.state.change, digits)} (${signed(row.state.change_pct, 2, "%")})`,
      row.state.timestamp ? `As of ${etTime(row.state.timestamp)}${row.state.source ? ` · ${row.state.source}` : ""}` : "Time unavailable",
    ] : []),
    BADGE_LINE[badge],
  ];
}

type Draft = { name: string; symbols: string[] };

const same = (a: Draft, b: Draft) => a.name.trim() === b.name.trim()
  && a.symbols.length === b.symbols.length && a.symbols.every((s, i) => s === b.symbols[i]);

export function WatchlistsPage() {
  const [saved, setSaved] = useState<Watchlist | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [adding, setAdding] = useState("");
  const [addError, setAddError] = useState<string | null>(null);
  const instruments = usePoll((signal) => fetchChartInstruments(signal), "instruments", 3_600_000);
  const quotes = usePoll((signal) => fetchQuotes(signal), "quotes", QUOTES_POLL_MS);
  const library = useExplainers();

  useEffect(() => {
    const controller = new AbortController();
    fetchWatchlist(controller.signal).then(
      (list) => { setSaved(list); setDraft({ name: list.name, symbols: list.symbols }); },
      (err: Error) => { if (!controller.signal.aborted) setLoadError(err.message || "Unavailable"); });
    return () => controller.abort();
  }, []);

  const registry = useMemo(() => new Map((instruments.data?.instruments ?? []).map((i) => [i.symbol, i])), [instruments.data]);
  const quoteBy = useMemo(() => new Map((quotes.data?.instruments ?? []).map((q) => [q.symbol, q])), [quotes.data]);

  if (!draft || !saved) return <main className="t-page wl-page">
    <section className="t-panel" aria-label="Watchlist">
      {loadError ? <p className="t-error" role="alert">Watchlist unavailable: {loadError}</p>
        : <p className="t-muted" role="status">Loading the watchlist…</p>}
    </section>
  </main>;

  const known = registry.size > 0;
  const dirty = !same(draft, saved);
  const nameOk = draft.name.trim().length > 0;
  const canSave = dirty && nameOk && draft.symbols.length > 0 && !saving;
  const choices = [...registry.keys()].filter((s) => !draft.symbols.includes(s));

  const edit = (next: Draft) => {
    setDraft(next);
    setNotice(null);
    setSaveError(null);
  };
  const move = (index: number, by: -1 | 1) => {
    const symbols = [...draft.symbols];
    [symbols[index], symbols[index + by]] = [symbols[index + by], symbols[index]];
    edit({ ...draft, symbols });
  };
  const remove = (symbol: string) => edit({ ...draft, symbols: draft.symbols.filter((s) => s !== symbol) });

  const add = (event: FormEvent) => {
    event.preventDefault();
    const symbol = adding.trim().toUpperCase();
    if (!symbol) return;
    if (draft.symbols.includes(symbol)) return setAddError(`${symbol} is already on the list.`);
    if (draft.symbols.length >= MAX_SYMBOLS) return setAddError(`A watchlist holds at most ${MAX_SYMBOLS} instruments.`);
    // With the registry loaded an unknown symbol is caught here; without it the core's 422 says so on save.
    if (known && !registry.has(symbol)) return setAddError(`${symbol} isn't an instrument Omarkets knows. Pick one from the list.`);
    setAddError(null);
    setAdding("");
    edit({ ...draft, symbols: [...draft.symbols, symbol] });
  };

  const save = async () => {
    if (!canSave) return;
    setSaving(true);
    setSaveError(null);
    try {
      const next = await saveWatchlist({ name: draft.name.trim(), symbols: draft.symbols });
      setSaved(next);
      setDraft({ name: next.name, symbols: next.symbols });
      setNotice(`Saved${next.updated_at ? ` at ${etTime(next.updated_at)}` : ""}. The Chart picker lists these first.`);
    } catch (err) {
      setSaveError((err as Error).message || "Could not save");
    } finally {
      setSaving(false);
    }
  };

  return <main className="t-page wl-page">
    <section className="t-panel" aria-label="Watchlist">
      <header className="wl-head">
        <label className="wl-name">
          <span>Name</span>
          <input type="text" aria-label="Watchlist name" value={draft.name} maxLength={MAX_NAME}
            onChange={(e) => edit({ ...draft, name: e.target.value })} />
        </label>
        <div className="wl-actions">
          <button type="button" onClick={() => edit({ name: saved.name, symbols: saved.symbols })} disabled={!dirty || saving}>
            Undo changes
          </button>
          <button type="button" className="t-primary" onClick={() => void save()} disabled={!canSave}>
            {saving ? "Saving…" : "Save"}
          </button>
        </div>
      </header>
      {!nameOk && <p className="t-error" role="alert">Give the list a name before saving.</p>}
      {saveError && <p className="t-error" role="alert">Couldn't save the watchlist: {saveError}</p>}
      {notice && <p className="t-muted" role="status">{notice}</p>}
      {quotes.error && <p className="t-muted" role="status">Quotes unavailable ({quotes.error}){quotes.data ? " · showing the last ones" : ""}</p>}
      {instruments.error && <p className="t-muted" role="status">Instrument list unavailable ({instruments.error}) · the core checks symbols when you save</p>}

      {draft.symbols.length === 0 ? <p className="t-muted wl-empty">The list is empty. Add at least one instrument to save it.</p>
        : <table className="status-table wl-table" aria-label={`${draft.name.trim() || "Watchlist"} instruments`}>
          <thead><tr><th>#</th><th>Instrument</th><th>Last</th><th>Change</th><th>%</th><th>Feed</th><th><span className="visually-hidden">Order and remove</span></th></tr></thead>
          <tbody>{draft.symbols.map((symbol, index) => {
            const inst = registry.get(symbol);
            const row = quoteBy.get(symbol);
            const badge = badgeOf(row);
            const digits = inst?.price_precision ?? 2;
            const unknown = known && !inst;
            return <tr key={symbol} className={unknown ? "wl-unknown" : undefined}>
              <td className="t-muted">{index + 1}</td>
              <td>
                <Explain title={inst?.label ?? symbol} lines={rowLines(symbol, inst, row, library?.get(`instrument:${symbol}`))}>
                  <b>{symbol}</b> {inst ? <span className="t-muted">{inst.label}</span> : unknown && <span className="wl-warn">not a known instrument</span>}
                </Explain>
              </td>
              <td className="wl-num">{badge === "none" ? "—" : fixed(row!.state.value, digits)}</td>
              <td className={`wl-num t-chg ${badge === "closed" ? "" : direction(row?.state.change ?? null)}`}>
                {badge === "none" ? "—" : signed(row!.state.change, digits)}</td>
              <td className={`wl-num t-chg ${badge === "closed" ? "" : direction(row?.state.change_pct ?? null)}`}>
                {badge === "none" ? "—" : signed(row!.state.change_pct, 2, "%")}</td>
              <td><span className={`wl-badge wl-${badge}`}>{BADGE_TEXT[badge]}</span></td>
              <td className="wl-row-actions">
                <button type="button" className="t-link" aria-label={`Move ${symbol} up`} disabled={index === 0} onClick={() => move(index, -1)}>↑</button>
                <button type="button" className="t-link" aria-label={`Move ${symbol} down`} disabled={index === draft.symbols.length - 1} onClick={() => move(index, 1)}>↓</button>
                <button type="button" className="t-link" aria-label={`Remove ${symbol}`} onClick={() => remove(symbol)}>✕</button>
              </td>
            </tr>;
          })}</tbody>
        </table>}

      <form className="wl-add" onSubmit={add}>
        <label>
          <span>Add an instrument</span>
          <input type="text" aria-label="Add an instrument" list="wl-choices" value={adding} placeholder="e.g. BTC"
            onChange={(e) => { setAdding(e.target.value); setAddError(null); }} />
        </label>
        <datalist id="wl-choices">{choices.map((s) => <option key={s} value={s}>{registry.get(s)!.label}</option>)}</datalist>
        <button type="submit" disabled={!adding.trim()}>Add</button>
      </form>
      {addError && <p className="t-error" role="alert">{addError}</p>}
      <p className="t-footnote">
        {draft.symbols.length} of {MAX_SYMBOLS} · Changes stay here until you press Save. Saving writes no note and spends nothing on AI.
        {quotes.data ? ` Quotes as of ${etTime(quotes.data.generated_at)}; changes are since the previous close.` : ""}
      </p>
    </section>
  </main>;
}
