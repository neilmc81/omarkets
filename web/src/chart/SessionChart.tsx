import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
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
import { NoteLink, useCardDismiss } from "../shell/Explain";
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

/** Overnight stretches to shade: everything in a session outside the cash hours.
 * A session without cash hours (24x7 crypto) has nothing to shade. */
export function overnightRanges(sessions: ChartSession[]): Array<{ from: number; to: number }> {
  return sessions.flatMap((s) => s.rth_opens_at === null || s.rth_closes_at === null ? [] : [
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

type Tip = { kind: "pin"; pin: Pin; x: number; y: number } | { kind: "level"; level: KeyLevel; x: number; y: number };

const sameTarget = (a: Tip, b: Tip) =>
  a.kind === "pin" ? b.kind === "pin" && a.pin.at === b.pin.at && a.pin.title === b.pin.title
    : b.kind === "level" && a.level.key === b.level.key;

export function SessionChart({ bars, levels = [], pins = [], overlays = [], height, label, onStatus, precision }: {
  bars: Bars | null;
  levels?: KeyLevel[];
  pins?: Pin[];
  overlays?: Overlay[];
  height: number;
  label: string;
  /** Called with the bar under the pointer's one-line readout (ET time and OHLC). */
  onStatus?: (text: string | null) => void;
  /** Decimals the core says the instrument quotes; the chart's default (2) when unset. */
  precision?: number;
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
  const root = useRef<HTMLDivElement>(null);
  const cardRef = useRef<HTMLDivElement>(null);
  // The open card (click-only, like <Explain>) and whether the pointer is on
  // something that would open one (for the cursor cue).
  const [tip, setTip] = useState<Tip | null>(null);
  const [aiming, setAiming] = useState(false);
  // The chart canvas has no keyboard target to hand focus back to, so focus
  // inside a closing card returns to the chart frame instead of the page.
  const close = useCallback(() => {
    if (cardRef.current?.contains(document.activeElement)) root.current?.focus({ preventScroll: true });
    setTip(null);
  }, []);
  useCardDismiss(tip !== null, close, root);

  // Latest data for the crosshair handler without re-subscribing.
  const latest = useRef({ bars, levels, pins, onStatus, precision });
  useEffect(() => { latest.current = { bars, levels, pins, onStatus, precision }; });

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

    // The pin or level line under the pointer, if any.
    const targetAt = (param: MouseEventParams<Time>): Tip | null => {
      const { bars: data, levels: lines, pins: marks } = latest.current;
      if (param.time === undefined || !param.point || !data) return null;
      const time = param.time as number;
      const { x, y } = param.point;
      const times = data.bars.map((b) => seconds(b.time));
      const pin = marks.find((p) => pinBarTime(p, times, STEP[data.interval]) === time);
      if (pin && y < 48) return { kind: "pin", pin, x, y };
      const level = lines.find((l) => {
        if (l.value === null) return false;
        const at = candles.priceToCoordinate(l.value);
        return at !== null && Math.abs(at - y) <= 6;
      });
      if (level) return { kind: "level", level, x, y };
      return pin ? { kind: "pin", pin, x, y } : null;
    };

    // Moving the pointer only updates the readout and the cursor; it never opens a card.
    const onMove = (param: MouseEventParams<Time>) => {
      const { bars: data, onStatus: status, precision: digits } = latest.current;
      setAiming(targetAt(param) !== null);
      if (param.time === undefined || !param.point || !data) {
        status?.(null);
        return;
      }
      const time = param.time as number;
      const bar = data.bars.find((b) => seconds(b.time) === time);
      status?.(bar && bar.count ? `${etDayClock(time)} ET · O ${fmt(bar.open, digits)} H ${fmt(bar.high, digits)} L ${fmt(bar.low, digits)} C ${fmt(bar.close, digits)}${bar.delayed ? " · delayed" : ""}`
        : `${etDayClock(time)} ET · no quotes in this bar`);
    };
    chart.subscribeCrosshairMove(onMove);
    // A click on a pin or level opens its card; a second click on it, or a
    // click on empty chart, closes it.
    const onClick = (param: MouseEventParams<Time>) => {
      const next = targetAt(param);
      setTip((open) => next && !(open && sameTarget(open, next)) ? next : null);
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

  // Keep the card inside the chart: right of and below the click when it
  // fits, flipped left or above otherwise. Placed again whenever the card
  // changes size (the explainer library arriving swaps in longer text).
  useLayoutEffect(() => {
    const card = cardRef.current;
    const box = root.current;
    if (!tip || !card || !box) return;
    const place = () => {
      const { width, height: tall } = card.getBoundingClientRect();
      const right = tip.x + 12 + width <= box.clientWidth - 4;
      const below = tip.y + 12 + tall <= box.clientHeight - 4;
      card.style.left = `${Math.max(4, right ? tip.x + 12 : tip.x - 12 - width)}px`;
      card.style.top = `${Math.max(4, below ? tip.y + 12 : tip.y - 12 - tall)}px`;
    };
    place();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(place);
    observer.observe(card);
    return () => observer.disconnect();
  }, [tip, library]);

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

  // The price scale's decimals follow the instrument (e.g. three for yields).
  useEffect(() => {
    if (precision === undefined) return;
    candlesRef.current?.applyOptions({ priceFormat: { type: "price", precision, minMove: 1 / 10 ** precision } });
  }, [precision]);

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

  const card = tip && <div ref={cardRef} className="explain-card chart-card" tabIndex={0}
    role={tip.kind === "pin" && tip.pin.note_id ? "dialog" : "tooltip"}
    aria-label={tip.kind === "pin" ? pinTitle(tip.pin) : `${tip.level.label} · ${tip.level.value_text}`}
    style={{ left: Math.max(4, tip.x + 12), top: Math.max(4, tip.y + 12) }}>
    {tip.kind === "pin" ? <>
      <b className="explain-title">{pinTitle(tip.pin)}</b>
      {pinLines(tip.pin).map((line) => <p key={line}>{line}</p>)}
      {tip.pin.note_id && <p className="explain-links"><NoteLink noteId={tip.pin.note_id} /></p>}
    </> : <>
      <b className="explain-title">{tip.level.label} · {tip.level.value_text}</b>
      {levelLines(tip.level, library?.get(`level:${tip.level.key}`)).map((line) => <p key={line}>{line}</p>)}
    </>}
  </div>;

  const empty = bars && !bars.bars.some((b) => b.count);
  return <div ref={root} className={`session-chart${aiming ? " is-aiming" : ""}`} style={{ height }} tabIndex={-1}>
    <div ref={container} className="session-chart-canvas" role="img" aria-label={label} />
    {card}
    {empty && <p className="session-chart-empty" role="status">No stored quotes for this range yet.</p>}
  </div>;
}

function fmt(value: number | null, digits = 2): string {
  return value === null ? "—" : value.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
