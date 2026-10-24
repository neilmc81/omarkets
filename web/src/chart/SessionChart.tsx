import { useEffect, useRef, useState } from "react";
import {
  CandlestickSeries,
  ColorType,
  createChart,
  createSeriesMarkers,
  LineSeries,
  LineStyle,
  TickMarkType,
  type IChartApi,
  type IPriceLine,
  type IPrimitivePaneView,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type ISeriesPrimitive,
  type MouseEventParams,
  type SeriesAttachedParameter,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { NoteLink } from "../shell/Explain";
import { chartPalette, THEME_EVENT, type ChartPalette } from "../theme/omarchy";
import type { Bar, Bars, ChartSession, KeyLevel, Pin } from "./api";
import { useExplainers } from "../explainers/library";
import { etClock, etDayClock, etWeekday, levelLines, pinLines, pinTitle } from "./explain";

// The session chart: candles from the core's bars, overnight shading from the
// core's session calendar, level lines, release pins and optional % overlays.
// It draws what the core computed and holds no market logic.

/** Which of the palette's three overlay colours a line uses. */
export type OverlaySlot = 0 | 1 | 2;
export type Overlay = { key: string; label: string; slot: OverlaySlot; bars: Bars };

const seconds = (iso: string) => Math.floor(Date.parse(iso) / 1000);

/** Bars as candles; a gap bar becomes whitespace so the gap stays visible. */
export function candleData(bars: Bar[]) {
  return bars.map((bar) => bar.count && bar.open !== null && bar.high !== null && bar.low !== null && bar.close !== null
    ? { time: seconds(bar.time) as UTCTimestamp, open: bar.open, high: bar.high, low: bar.low, close: bar.close }
    : { time: seconds(bar.time) as UTCTimestamp });
}

/** An overlay's % change from the start of the range, as the core computed it. */
export function overlayData(bars: Bar[]) {
  return bars.map((bar) => bar.change_pct === null
    ? { time: seconds(bar.time) as UTCTimestamp }
    : { time: seconds(bar.time) as UTCTimestamp, value: bar.change_pct });
}

/** The bar a pin belongs to: the one whose interval holds the release time. */
export function pinBarTime(pin: Pin, barTimes: number[], step: number): number | null {
  const at = seconds(pin.at);
  for (const time of barTimes) if (time <= at && at < time + step) return time;
  return null;
}

/** Overnight stretches to shade: everything in a session outside the cash hours. */
export function overnightRanges(sessions: ChartSession[]): Array<{ from: number; to: number }> {
  return sessions.flatMap((s) => [
    { from: seconds(s.opens_at), to: seconds(s.rth_opens_at) },
    { from: seconds(s.rth_closes_at), to: seconds(s.closes_at) },
  ]).filter((range) => range.to > range.from);
}

const STEP: Record<Bars["interval"], number> = { "5m": 300, "15m": 900, "1h": 3600 };

const LEVEL_TONE: Record<KeyLevel["key"], "up" | "down" | "muted"> = {
  overnight_high: "up", prior_day_high: "up", prior_week_high: "up",
  overnight_low: "down", prior_day_low: "down", prior_week_low: "down",
  prior_settle: "muted", gap_vs_settle: "muted",
};

// Shades the overnight stretches behind the candles and labels each one.
class OvernightShade implements ISeriesPrimitive<Time> {
  private param: SeriesAttachedParameter<Time> | null = null;
  private ranges: Array<{ from: number; to: number }> = [];
  private times: number[] = [];
  private colors: { fill: string; text: string } = { fill: "transparent", text: "transparent" };

  attached(param: SeriesAttachedParameter<Time>) { this.param = param; }
  detached() { this.param = null; }

  update(ranges: Array<{ from: number; to: number }>, times: number[], palette: ChartPalette) {
    this.ranges = ranges;
    this.times = times;
    this.colors = { fill: palette.shade, text: palette.muted };
    this.param?.requestUpdate();
  }

  paneViews(): readonly IPrimitivePaneView[] {
    return [{
      zOrder: () => "bottom",
      renderer: () => ({
        draw: () => undefined,
        drawBackground: (target) => {
          const param = this.param;
          if (!param) return;
          const scale = param.chart.timeScale();
          const half = scale.options().barSpacing / 2;
          target.useMediaCoordinateSpace(({ context, mediaSize }) => {
            context.font = "11px Inter, ui-sans-serif, sans-serif";
            for (const range of this.ranges) {
              const inside = this.times.filter((t) => t >= range.from && t < range.to);
              if (!inside.length) continue;
              const x1 = scale.timeToCoordinate(inside[0] as UTCTimestamp);
              const x2 = scale.timeToCoordinate(inside[inside.length - 1] as UTCTimestamp);
              if (x1 === null || x2 === null) continue;
              const left = Math.max(0, x1 - half);
              const right = Math.min(mediaSize.width, x2 + half);
              if (right <= left) continue;
              context.fillStyle = this.colors.fill;
              context.fillRect(left, 0, right - left, mediaSize.height);
              if (right - left > 60) {
                context.fillStyle = this.colors.text;
                context.fillText("Overnight", left + 8, 16);
              }
            }
          });
        },
      }),
    }];
  }
}

type Hover = { kind: "pin"; pin: Pin; x: number; y: number } | { kind: "level"; level: KeyLevel; x: number; y: number };

export function SessionChart({ bars, levels = [], pins = [], overlays = [], height, label, onStatus }: {
  bars: Bars | null;
  levels?: KeyLevel[];
  pins?: Pin[];
  overlays?: Overlay[];
  height: number;
  label: string;
  /** Called with the hovered bar's one-line readout (ET time and OHLC). */
  onStatus?: (text: string | null) => void;
}) {
  const library = useExplainers();
  const container = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candlesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const shadeRef = useRef<OvernightShade | null>(null);
  const linesRef = useRef<IPriceLine[]>([]);
  const overlaySeries = useRef(new Map<string, ISeriesApi<"Line">>());
  const fittedKey = useRef<string | null>(null);
  const interacted = useRef(false);
  const [palette, setPalette] = useState<ChartPalette>(() => chartPalette());
  const [hover, setHover] = useState<Hover | null>(null);
  const [pinned, setPinned] = useState(false);
  const hoverRef = useRef<Hover | null>(null);
  useEffect(() => { hoverRef.current = hover; }, [hover]);

  // Latest data for the crosshair handler without re-subscribing.
  const latest = useRef({ bars, levels, pins, onStatus, pinned });
  useEffect(() => { latest.current = { bars, levels, pins, onStatus, pinned }; });

  useEffect(() => {
    const element = container.current;
    if (!element) return;
    const colors = chartPalette();
    const chart = createChart(element, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: colors.background }, textColor: colors.text,
        fontFamily: "ui-monospace, monospace", attributionLogo: false },
      grid: { vertLines: { color: colors.grid }, horzLines: { color: colors.grid } },
      rightPriceScale: { borderColor: colors.border },
      leftPriceScale: { borderColor: colors.border, visible: false },
      crosshair: { vertLine: { color: colors.crosshair }, horzLine: { color: colors.crosshair } },
      timeScale: {
        borderColor: colors.border, timeVisible: true, secondsVisible: false,
        // The chart picks day-boundary ticks in UTC, so they are labelled with
        // the New York weekday and time ("Thu 20:00"), never a bare date.
        tickMarkFormatter: (time: Time, type: TickMarkType) => typeof time !== "number" ? null
          : type <= TickMarkType.DayOfMonth ? `${etWeekday(time)} ${etClock(time)}` : etClock(time),
      },
      localization: { locale: "en-US", timeFormatter: (time: Time) => typeof time === "number" ? `${etDayClock(time)} ET` : "" },
    });
    const candles = chart.addSeries(CandlestickSeries, {
      upColor: colors.up, downColor: colors.down, wickUpColor: colors.up, wickDownColor: colors.down,
      borderVisible: false, priceLineVisible: false,
    });
    const shade = new OvernightShade();
    candles.attachPrimitive(shade);
    chartRef.current = chart;
    candlesRef.current = candles;
    shadeRef.current = shade;
    markersRef.current = createSeriesMarkers(candles, []);
    fittedKey.current = null;
    interacted.current = false;

    const onMove = (param: MouseEventParams<Time>) => {
      const { bars: data, levels: lines, pins: marks, onStatus: status, pinned: sticky } = latest.current;
      if (sticky) return;
      if (param.time === undefined || !param.point || !data) {
        setHover(null);
        status?.(null);
        return;
      }
      const time = param.time as number;
      const step = STEP[data.interval];
      const bar = data.bars.find((b) => seconds(b.time) === time);
      status?.(bar && bar.count ? `${etDayClock(time)} ET · O ${fmt(bar.open)} H ${fmt(bar.high)} L ${fmt(bar.low)} C ${fmt(bar.close)}${bar.delayed ? " · delayed" : ""}`
        : `${etDayClock(time)} ET · no quotes in this bar`);
      const times = data.bars.map((b) => seconds(b.time));
      const pin = marks.find((p) => pinBarTime(p, times, step) === time);
      if (pin && param.point.y < 48) {
        setHover({ kind: "pin", pin, x: param.point.x, y: param.point.y });
        return;
      }
      const level = lines.find((l) => {
        if (l.value === null) return false;
        const y = candles.priceToCoordinate(l.value);
        return y !== null && Math.abs(y - param.point!.y) <= 6;
      });
      if (level) setHover({ kind: "level", level, x: param.point.x, y: param.point.y });
      else if (pin) setHover({ kind: "pin", pin, x: param.point.x, y: param.point.y });
      else setHover(null);
    };
    chart.subscribeCrosshairMove(onMove);
    // A click keeps the card open (so its note link can be used); another closes it.
    const onClick = () => {
      if (latest.current.pinned) {
        setPinned(false);
        setHover(null);
      } else if (hoverRef.current) setPinned(true);
    };
    chart.subscribeClick(onClick);
    const markInteracted = () => { interacted.current = true; };
    element.addEventListener("wheel", markInteracted, { passive: true });
    element.addEventListener("pointerdown", markInteracted);
    const retheme = () => setPalette(chartPalette());
    window.addEventListener(THEME_EVENT, retheme);
    // The theme may have been applied between the first render and now.
    retheme();
    const overlaysMap = overlaySeries.current;
    return () => {
      window.removeEventListener(THEME_EVENT, retheme);
      element.removeEventListener("wheel", markInteracted);
      element.removeEventListener("pointerdown", markInteracted);
      chart.unsubscribeCrosshairMove(onMove);
      chart.unsubscribeClick(onClick);
      chart.remove();
      chartRef.current = null;
      candlesRef.current = null;
      markersRef.current = null;
      shadeRef.current = null;
      linesRef.current = [];
      overlaysMap.clear();
    };
  }, []);

  useEffect(() => {
    if (!pinned) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") { setPinned(false); setHover(null); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [pinned]);

  // Theme colours.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    chart.applyOptions({
      layout: { background: { type: ColorType.Solid, color: palette.background }, textColor: palette.text },
      grid: { vertLines: { color: palette.grid }, horzLines: { color: palette.grid } },
      rightPriceScale: { borderColor: palette.border },
      leftPriceScale: { borderColor: palette.border },
      timeScale: { borderColor: palette.border },
      crosshair: { vertLine: { color: palette.crosshair }, horzLine: { color: palette.crosshair } },
    });
    candlesRef.current?.applyOptions({ upColor: palette.up, downColor: palette.down, wickUpColor: palette.up, wickDownColor: palette.down });
  }, [palette]);

  // Candles, shading and the first fit.
  useEffect(() => {
    const chart = chartRef.current;
    const candles = candlesRef.current;
    if (!chart || !candles) return;
    const data = bars?.bars ?? [];
    candles.setData(candleData(data));
    const times = data.map((b) => seconds(b.time));
    shadeRef.current?.update(overnightRanges(bars?.sessions ?? []), times, palette);
    const key = bars ? `${bars.symbol}:${bars.range}:${bars.interval}` : null;
    if (key && data.length && fittedKey.current !== key) {
      chart.timeScale().fitContent();
      fittedKey.current = key;
      interacted.current = false;
    }
  }, [bars, palette]);

  // Level lines.
  useEffect(() => {
    const candles = candlesRef.current;
    if (!candles) return;
    for (const line of linesRef.current) candles.removePriceLine(line);
    const tone = { up: palette.up, down: palette.down, muted: palette.muted };
    linesRef.current = levels.filter((l) => l.value !== null && l.key !== "gap_vs_settle").map((l) => candles.createPriceLine({
      price: l.value as number, color: tone[LEVEL_TONE[l.key]], lineWidth: 1, lineStyle: LineStyle.Dashed,
      axisLabelVisible: true, title: l.label,
    }));
  }, [levels, palette]);

  // Release pins, on the bar that holds the release time.
  useEffect(() => {
    const markers = markersRef.current;
    if (!markers) return;
    const data = bars?.bars ?? [];
    const times = data.filter((b) => b.count).map((b) => seconds(b.time));
    const step = bars ? STEP[bars.interval] : 300;
    markers.setMarkers(pins.flatMap((pin) => {
      const time = pinBarTime(pin, times, step);
      return time === null ? [] : [{
        time: time as UTCTimestamp, position: "aboveBar" as const, shape: "circle" as const,
        color: pin.importance === "high" ? palette.down : palette.warn, size: 1,
      }];
    }));
  }, [bars, pins, palette]);

  // Overlays as % change, on the left scale.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const series = overlaySeries.current;
    for (const [key, line] of series) {
      if (!overlays.some((o) => o.key === key)) {
        chart.removeSeries(line);
        series.delete(key);
      }
    }
    for (const overlay of overlays) {
      let line = series.get(overlay.key);
      if (!line) {
        line = chart.addSeries(LineSeries, {
          priceScaleId: "left", lineWidth: 2, priceLineVisible: false, lastValueVisible: true, title: overlay.label,
          priceFormat: { type: "custom", minMove: 0.01, formatter: (value: number) => `${value >= 0 ? "+" : ""}${value.toFixed(2)}%` },
        });
        series.set(overlay.key, line);
      }
      line.applyOptions({ color: palette.overlays[overlay.slot] });
      line.setData(overlayData(overlay.bars.bars));
    }
    chart.applyOptions({ leftPriceScale: { visible: overlays.length > 0 } });
  }, [overlays, palette]);

  const card = hover && <div className={`explain-card chart-card ${pinned ? "is-pinned" : ""}`} role={pinned ? "dialog" : "tooltip"}
    style={{ left: Math.max(4, hover.x + 12), top: Math.max(4, hover.y + 12) }}>
    {hover.kind === "pin" ? <>
      <b className="explain-title">{pinTitle(hover.pin)}</b>
      {pinLines(hover.pin).map((line) => <p key={line}>{line}</p>)}
      {hover.pin.note_id && <p className="explain-links"><NoteLink noteId={hover.pin.note_id} /></p>}
    </> : <>
      <b className="explain-title">{hover.level.label} · {hover.level.value_text}</b>
      {levelLines(hover.level, library?.get(`level:${hover.level.key}`)).map((line) => <p key={line}>{line}</p>)}
    </>}
    {!pinned && hover.kind === "pin" && hover.pin.note_id && <p className="t-muted">Click to keep this open.</p>}
  </div>;

  const empty = bars && !bars.bars.some((b) => b.count);
  return <div className="session-chart" style={{ height }}>
    <div ref={container} className="session-chart-canvas" role="img" aria-label={label} />
    {card}
    {empty && <p className="session-chart-empty" role="status">No stored quotes for this range yet.</p>}
  </div>;
}

function fmt(value: number | null): string {
  return value === null ? "—" : value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
