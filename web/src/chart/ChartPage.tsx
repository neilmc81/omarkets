import { useEffect, useMemo, useState } from "react";
import { Explain } from "../shell/Explain";
import { focusSaves, NQ_FALLBACK, queueFocusSave } from "../shell/focus";
import { ChartFooter, ChartState } from "../today/Market";
import {
  fetchBars, fetchChartInstruments, fetchFocus, fetchLevels, fetchPins, fetchWatchlist, intervalsFor, RANGES,
  type ChartInstrument, type ChartRange, type Interval,
} from "./api";
import { useExplainers } from "../explainers/library";
import { levelLines, pinLines, pinTitle } from "./explain";
import { chartPalette, THEME_EVENT } from "../theme/omarchy";
import { SessionChart, type Overlay, type OverlaySlot } from "./SessionChart";
import { usePoll } from "./usePoll";

// The Chart page: the focus instrument (NQ by default, saved by the core) over
// 1D / 5D / 1M with level lines (NQ, ES), release pins (NQ only) and optional
// overlays (ES, the 2-year yield, the dollar) as % change from the start of the
// range. Bars, levels, pins, % changes, labels and precision all come from the
// core. The focus is global: Today and the header follow it (shell/focus.ts);
// the desk and its notes do not.

const OVERLAYS: Array<{ symbol: string; label: string; slot: OverlaySlot }> = [
  { symbol: "ES", label: "S&P 500 futures", slot: 0 },
  { symbol: "US2Y", label: "2-year yield", slot: 1 },
  { symbol: "DXY", label: "US dollar", slot: 2 },
];

// The overlay colours the chart uses, for the toggles' underlines.
function useOverlayColors(): [string, string, string] {
  const [colors, setColors] = useState(() => chartPalette().overlays);
  useEffect(() => {
    const update = () => setColors(chartPalette().overlays);
    window.addEventListener(THEME_EVENT, update);
    update();
    return () => window.removeEventListener(THEME_EVENT, update);
  }, []);
  return colors;
}
const RANGE_LABEL: Record<ChartRange, string> = { "1d": "this session", "5d": "5 sessions", "1m": "1 month" };
const POLL: Record<ChartRange, number> = { "1d": 60_000, "5d": 120_000, "1m": 300_000 };

function useOverlay(symbol: string, on: boolean, interval: Interval, range: ChartRange) {
  // Off overlays poll nothing: their key never changes and the loader resolves null.
  return usePoll((signal) => on ? fetchBars(symbol, interval, range, signal) : Promise.resolve(null),
    on ? `${symbol}:${interval}:${range}` : `${symbol}:off`, on ? POLL[range] : 3_600_000);
}

export function ChartPage() {
  // A choice still being saved is the focus; otherwise wait for the core's.
  const [focus, setFocus] = useState<ChartInstrument | null>(() => focusSaves.pending);
  const [focusError, setFocusError] = useState<string | null>(null);
  const instruments = usePoll((signal) => fetchChartInstruments(signal), "instruments", 3_600_000);
  const watchlist = usePoll((signal) => fetchWatchlist(signal), "watchlist", 3_600_000);

  useEffect(() => {
    const controller = new AbortController();
    // Read only after any saves in flight, so the answer is never older than them.
    focusSaves.queue.then(() => fetchFocus(controller.signal)).then(
      (saved) => setFocus((current) => current ?? saved.instrument),
      (err: Error) => {
        if (controller.signal.aborted) return;
        setFocusError(`Chart focus unavailable (${err.message}) · showing NQ`);
        setFocus((current) => current ?? NQ_FALLBACK);
      });
    return () => controller.abort();
  }, []);

  const pick = (next: ChartInstrument) => {
    setFocus(next);
    // One save at a time, in order, so the last choice is the one kept.
    queueFocusSave(next).then(
      () => setFocusError(null),
      (err: Error) => setFocusError(`Couldn't save the chart focus (${err.message})`));
  };

  if (!focus) return <main className="t-page chart-page">
    <section className="t-panel" aria-label="Chart"><p className="t-muted" role="status">Loading bars…</p></section>
  </main>;
  return <FocusChart focus={focus} choices={instruments.data?.instruments ?? null} watchlist={watchlist.data}
    pickerError={instruments.error} focusError={focusError} onPick={pick} />;
}

function Picker({ focus, choices, watchlist, onPick }: {
  focus: ChartInstrument;
  choices: ChartInstrument[] | null;
  watchlist: { name: string; symbols: string[] } | null;
  onPick: (next: ChartInstrument) => void;
}) {
  const all = choices ?? [focus];
  const bySymbol = new Map(all.map((c) => [c.symbol, c]));
  if (!bySymbol.has(focus.symbol)) bySymbol.set(focus.symbol, focus);
  const listed = (watchlist?.symbols ?? []).filter((s) => bySymbol.has(s));
  const others = [...bySymbol.keys()].filter((s) => !listed.includes(s));
  const option = (symbol: string) => <option key={symbol} value={symbol}>{symbol} · {bySymbol.get(symbol)!.label}</option>;
  return <select aria-label="Instrument" value={focus.symbol} disabled={!choices}
    onChange={(e) => { const next = bySymbol.get(e.target.value); if (next) onPick(next); }}>
    {listed.length > 0 && <optgroup label={watchlist!.name}>{listed.map(option)}</optgroup>}
    {listed.length > 0 ? <optgroup label="Other instruments">{others.map(option)}</optgroup> : others.map(option)}
  </select>;
}

function FocusChart({ focus, choices, watchlist, pickerError, focusError, onPick }: {
  focus: ChartInstrument;
  choices: ChartInstrument[] | null;
  watchlist: { name: string; symbols: string[] } | null;
  pickerError: string | null;
  focusError: string | null;
  onPick: (next: ChartInstrument) => void;
}) {
  const library = useExplainers();
  const symbol = focus.symbol;
  const [range, setRange] = useState<ChartRange>("5d");
  const [interval, setBarSize] = useState<Interval>("15m");
  const [on, setOn] = useState<Record<string, boolean>>({ ES: false, US2Y: false, DXY: false });
  const [status, setStatus] = useState<string | null>(null);
  // A new instrument starts with no hovered bar's readout from the old one.
  useEffect(() => setStatus(null), [symbol]);
  const colors = useOverlayColors();
  const pickRange = (next: ChartRange) => {
    setRange(next);
    if (!intervalsFor(next).includes(interval)) setBarSize("15m");
  };
  const bars = usePoll((signal) => fetchBars(symbol, interval, range, signal), `${symbol}:${interval}:${range}`, POLL[range]);
  // Pins and levels are fetched only where the core says they exist.
  const pins = usePoll((signal) => focus.release_pins ? fetchPins(range, signal) : Promise.resolve(null),
    focus.release_pins ? `${symbol}:${range}` : `${symbol}:off`, focus.release_pins ? 300_000 : 3_600_000);
  const levels = usePoll((signal) => focus.levels ? fetchLevels(symbol, signal) : Promise.resolve(null),
    focus.levels ? symbol : `${symbol}:off`, focus.levels ? 60_000 : 3_600_000);
  const overlayList = OVERLAYS.filter((o) => o.symbol !== symbol);
  const es = useOverlay("ES", on.ES && symbol !== "ES", interval, range);
  const us2y = useOverlay("US2Y", on.US2Y && symbol !== "US2Y", interval, range);
  const dxy = useOverlay("DXY", on.DXY && symbol !== "DXY", interval, range);
  const loaded = { ES: es.data, US2Y: us2y.data, DXY: dxy.data } as const;
  const overlays: Overlay[] = useMemo(() => OVERLAYS.flatMap((o) => {
    const data = loaded[o.symbol as keyof typeof loaded];
    return o.symbol !== symbol && on[o.symbol] && data ? [{ key: o.symbol, label: o.label, slot: o.slot, bars: data }] : [];
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [on, symbol, es.data, us2y.data, dxy.data]);
  const overlayErrors = [es, us2y, dxy].map((o, i) => o.error ? `${OVERLAYS[i].label}: ${o.error}` : null).filter(Boolean);
  // Only ever draw the focus instrument's own data, never a previous one's.
  const shown = { ...bars, data: bars.data?.symbol === symbol ? bars.data : null };
  const lines = focus.levels && levels.data?.symbol === symbol ? levels.data.levels.filter((l) => l.key !== "gap_vs_settle") : [];
  const pinList = focus.release_pins ? pins.data?.pins ?? [] : [];

  return <main className="t-page chart-page">
    <section className="t-panel" aria-label={`${symbol} chart`}>
      <header className="chart-toolbar">
        <Picker focus={focus} choices={choices} watchlist={watchlist} onPick={onPick} />
        <b className="chart-symbol">{symbol} · {interval}</b>
        <span className="seg-buttons" role="group" aria-label="Range">
          {RANGES.map((r) => <button key={r} aria-pressed={range === r} onClick={() => pickRange(r)}>{r.toUpperCase()}</button>)}
        </span>
        <select aria-label="Bar size" value={interval} onChange={(e) => setBarSize(e.target.value as Interval)}>
          {intervalsFor(range).map((i) => <option key={i} value={i}>{i}</option>)}
        </select>
        <span className="chart-overlays" role="group" aria-label="Overlays">
          <span className="t-muted">Overlay:</span>
          {overlayList.map((o) => <label key={o.symbol} className="overlay-toggle" style={{ borderBottomColor: colors[o.slot] }}>
            <input type="checkbox" checked={on[o.symbol]} onChange={(e) => setOn((current) => ({ ...current, [o.symbol]: e.target.checked }))} />
            {o.label}
          </label>)}
        </span>
        <span className="t-muted chart-hint">Overlays shown as % change from the start of the range (left scale)</span>
      </header>
      {focusError && <p className="t-error" role="status">{focusError}</p>}
      {pickerError && <p className="t-error" role="status">Instrument list unavailable ({pickerError})</p>}
      <ChartState bars={shown} />
      {overlayErrors.length > 0 && <p className="t-error" role="status">Overlay unavailable · {overlayErrors.join(" · ")}</p>}
      {/* Keyed by instrument: a switch starts a fresh chart, so no hover card,
          pinned card or crosshair from the previous instrument survives. */}
      <SessionChart key={symbol} bars={shown.data} levels={lines} pins={pinList} overlays={overlays} height={560} onStatus={setStatus}
        precision={focus.price_precision}
        label={`${symbol} candles, ${RANGE_LABEL[range]}, ${interval} bars, times in New York.`} />
      <ChartFooter bars={shown.data} status={status} />
      <div className="chart-legend">
        <span className="t-muted">Levels:</span>
        {!focus.levels && <span className="t-muted">none for {focus.label}</span>}
        {lines.map((level) => <Explain key={level.key} title={`${level.label} · ${level.value_text}`} lines={levelLines(level, library?.get(`level:${level.key}`))}>
          {level.label} <b className={level.value === null ? "t-muted" : ""}>{level.value_text}</b>
        </Explain>)}
        {focus.levels && levels.error && <span className="t-muted">unavailable ({levels.error})</span>}
      </div>
      <div className="chart-legend">
        <span className="t-muted">Releases:</span>
        {!focus.release_pins && <span className="t-muted">shown on the NQ chart (each pin measures NQ's first 15 minutes)</span>}
        {focus.release_pins && pinList.length === 0 && <span className="t-muted">{pins.error ? `unavailable (${pins.error})` : "none in this range"}</span>}
        {pinList.map((pin) => <Explain key={pin.at + pin.title} title={pinTitle(pin)} lines={pinLines(pin)} noteId={pin.note_id}>
          <i className={`pin-dot imp-${pin.importance}`} aria-hidden="true" />{pinTitle(pin)}
        </Explain>)}
      </div>
    </section>
  </main>;
}
