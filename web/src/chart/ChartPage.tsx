import { useEffect, useMemo, useState } from "react";
import { Explain } from "../shell/Explain";
import { ChartFooter, ChartState } from "../today/Market";
import { fetchBars, fetchLevels, fetchPins, intervalsFor, RANGES, type ChartRange, type Interval } from "./api";
import { useExplainers } from "../explainers/library";
import { levelLines, pinLines, pinTitle } from "./explain";
import { chartPalette, THEME_EVENT } from "../theme/omarchy";
import { SessionChart, type Overlay, type OverlaySlot } from "./SessionChart";
import { usePoll } from "./usePoll";

// The Chart page: NQ over 1D / 5D / 1M with level lines, release pins and
// optional overlays (ES, the 2-year yield, the dollar) as % change from the
// start of the range. Bars, levels, pins and % changes all come from the core.

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
  const library = useExplainers();
  const [range, setRange] = useState<ChartRange>("5d");
  const [interval, setBarSize] = useState<Interval>("15m");
  const [on, setOn] = useState<Record<string, boolean>>({ ES: false, US2Y: false, DXY: false });
  const [status, setStatus] = useState<string | null>(null);
  const colors = useOverlayColors();
  const pick = (next: ChartRange) => {
    setRange(next);
    if (!intervalsFor(next).includes(interval)) setBarSize("15m");
  };
  const bars = usePoll((signal) => fetchBars("NQ", interval, range, signal), `NQ:${interval}:${range}`, POLL[range]);
  const pins = usePoll((signal) => fetchPins(range, signal), range, 300_000);
  const levels = usePoll((signal) => fetchLevels("NQ", signal), "NQ", 60_000);
  const es = useOverlay("ES", on.ES, interval, range);
  const us2y = useOverlay("US2Y", on.US2Y, interval, range);
  const dxy = useOverlay("DXY", on.DXY, interval, range);
  const loaded = { ES: es.data, US2Y: us2y.data, DXY: dxy.data } as const;
  const overlays: Overlay[] = useMemo(() => OVERLAYS.flatMap((o) => {
    const data = loaded[o.symbol as keyof typeof loaded];
    return on[o.symbol] && data ? [{ key: o.symbol, label: o.label, slot: o.slot, bars: data }] : [];
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [on, es.data, us2y.data, dxy.data]);
  const overlayErrors = [es, us2y, dxy].map((o, i) => o.error ? `${OVERLAYS[i].label}: ${o.error}` : null).filter(Boolean);
  const lines = levels.data?.levels.filter((l) => l.key !== "gap_vs_settle") ?? [];
  const pinList = pins.data?.pins ?? [];

  return <main className="t-page chart-page">
    <section className="t-panel" aria-label="NQ chart">
      <header className="chart-toolbar">
        <b className="chart-symbol">NQ · {interval}</b>
        <span className="seg-buttons" role="group" aria-label="Range">
          {RANGES.map((r) => <button key={r} aria-pressed={range === r} onClick={() => pick(r)}>{r.toUpperCase()}</button>)}
        </span>
        <select aria-label="Bar size" value={interval} onChange={(e) => setBarSize(e.target.value as Interval)}>
          {intervalsFor(range).map((i) => <option key={i} value={i}>{i}</option>)}
        </select>
        <span className="chart-overlays" role="group" aria-label="Overlays">
          <span className="t-muted">Overlay:</span>
          {OVERLAYS.map((o) => <label key={o.symbol} className="overlay-toggle" style={{ borderBottomColor: colors[o.slot] }}>
            <input type="checkbox" checked={on[o.symbol]} onChange={(e) => setOn((current) => ({ ...current, [o.symbol]: e.target.checked }))} />
            {o.label}
          </label>)}
        </span>
        <span className="t-muted chart-hint">Overlays shown as % change from the start of the range (left scale)</span>
      </header>
      <ChartState bars={bars} />
      {overlayErrors.length > 0 && <p className="t-error" role="status">Overlay unavailable · {overlayErrors.join(" · ")}</p>}
      <SessionChart bars={bars.data} levels={lines} pins={pinList} overlays={overlays} height={560} onStatus={setStatus}
        label={`NQ candles, ${RANGE_LABEL[range]}, ${interval} bars, times in New York.`} />
      <ChartFooter bars={bars.data} status={status} />
      <div className="chart-legend">
        <span className="t-muted">Levels:</span>
        {lines.map((level) => <Explain key={level.key} title={`${level.label} · ${level.value_text}`} lines={levelLines(level, library?.get(`level:${level.key}`))}>
          {level.label} <b className={level.value === null ? "t-muted" : ""}>{level.value_text}</b>
        </Explain>)}
        {levels.error && <span className="t-muted">unavailable ({levels.error})</span>}
      </div>
      <div className="chart-legend">
        <span className="t-muted">Releases:</span>
        {pinList.length === 0 && <span className="t-muted">{pins.error ? `unavailable (${pins.error})` : "none in this range"}</span>}
        {pinList.map((pin) => <Explain key={pin.at + pin.title} title={pinTitle(pin)} lines={pinLines(pin)} noteId={pin.note_id}>
          <i className={`pin-dot imp-${pin.importance}`} aria-hidden="true" />{pinTitle(pin)}
        </Explain>)}
      </div>
    </section>
  </main>;
}
