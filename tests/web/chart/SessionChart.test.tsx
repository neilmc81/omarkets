import { act, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { candleData, overlayData, overnightRanges, pinBarTime, SessionChart } from "../../../web/src/chart/SessionChart";
import { bars, esBars, levels, pins } from "../chartFixtures";

type Handler = (param: { time?: number; point?: { x: number; y: number } }) => void;
const lw = vi.hoisted(() => ({
  options: null as Record<string, unknown> | null,
  candles: { setData: vi.fn(), applyOptions: vi.fn(), attachPrimitive: vi.fn(), createPriceLine: vi.fn((o: unknown) => o), removePriceLine: vi.fn(), priceToCoordinate: vi.fn((price: number) => price === 29_220 ? 40 : 300) },
  lines: [] as Array<{ setData: ReturnType<typeof vi.fn>; applyOptions: ReturnType<typeof vi.fn>; options: Record<string, unknown> }>,
  markers: { setMarkers: vi.fn() },
  move: null as Handler | null,
  click: null as (() => void) | null,
  chart: { applyOptions: vi.fn(), remove: vi.fn(), removeSeries: vi.fn(), unsubscribeCrosshairMove: vi.fn(), unsubscribeClick: vi.fn(), fitContent: vi.fn() },
}));

vi.mock("lightweight-charts", () => ({
  ColorType: { Solid: "solid" },
  LineStyle: { Dashed: 2 },
  TickMarkType: { Year: 0, Month: 1, DayOfMonth: 2, Time: 3, TimeWithSeconds: 4 },
  CandlestickSeries: "Candlestick",
  LineSeries: "Line",
  createSeriesMarkers: () => lw.markers,
  createChart: (_element: unknown, options: Record<string, unknown>) => {
    lw.options = options;
    return {
      ...lw.chart,
      addSeries: (type: string, options: Record<string, unknown>) => {
        if (type === "Candlestick") return lw.candles;
        const line = { setData: vi.fn(), applyOptions: vi.fn(), options };
        lw.lines.push(line);
        return line;
      },
      subscribeCrosshairMove: (handler: Handler) => { lw.move = handler; },
      subscribeClick: (handler: () => void) => { lw.click = handler; },
      timeScale: () => ({ fitContent: lw.chart.fitContent, options: () => ({ barSpacing: 6 }), timeToCoordinate: () => 10 }),
    };
  },
}));

const seconds = (iso: string) => Date.parse(iso) / 1000;

beforeEach(() => {
  lw.lines = [];
  lw.move = null;
  for (const fn of [lw.candles.setData, lw.candles.createPriceLine, lw.candles.removePriceLine, lw.markers.setMarkers, lw.chart.fitContent]) fn.mockClear();
});

describe("SessionChart data mapping", () => {
  it("turns gap bars into whitespace, never zero candles", () => {
    const data = candleData(bars.bars);
    expect(data[1]).toEqual({ time: seconds("2026-10-22T12:30:00Z"), open: 29_118, high: 29_123, low: 29_116, close: 29_120 });
    expect(data[2]).toEqual({ time: seconds("2026-10-22T12:35:00Z") });
  });

  it("uses the core's % change for overlays", () => {
    expect(overlayData(esBars.bars).map((p) => ("value" in p ? p.value : null))).toEqual([0, -0.13]);
  });

  it("puts a pin on the bar that holds the release time", () => {
    const times = bars.bars.map((b) => seconds(b.time));
    expect(pinBarTime(pins.pins[0], times, 300)).toBe(seconds("2026-10-22T12:30:00Z"));
    expect(pinBarTime(pins.pins[1], times, 300)).toBeNull();
  });

  it("shades everything in a session outside the cash hours", () => {
    expect(overnightRanges(bars.sessions)).toEqual([
      { from: seconds("2026-10-21T22:00:00Z"), to: seconds("2026-10-22T13:30:00Z") },
      { from: seconds("2026-10-22T20:00:00Z"), to: seconds("2026-10-22T21:00:00Z") },
    ]);
  });
});

describe("SessionChart", () => {
  it("draws candles, level lines and pins from the core's data", () => {
    render(<SessionChart bars={bars} levels={levels.levels} pins={pins.pins} height={300} label="NQ chart" />);
    expect(screen.getByRole("img", { name: "NQ chart" })).toBeInTheDocument();
    expect(lw.candles.setData).toHaveBeenLastCalledWith(candleData(bars.bars));
    expect(lw.chart.fitContent).toHaveBeenCalledTimes(1);
    // Every level with a value, except the gap (it is a distance, not a price).
    // Redrawing (e.g. after a theme change) removes the old lines first.
    const created = lw.candles.createPriceLine.mock.calls.length;
    expect(lw.candles.removePriceLine).toHaveBeenCalledTimes(created - 6);
    const titles = lw.candles.createPriceLine.mock.calls.slice(-6).map(([o]) => (o as { title: string }).title);
    expect(titles).toEqual(["Overnight high", "Overnight low", "Prior settle", "Prior day low", "Prior week high", "Prior week low"]);
    const [markers] = lw.markers.setMarkers.mock.calls.at(-1)!;
    expect(markers).toHaveLength(1);
    expect(markers[0]).toMatchObject({ time: seconds("2026-10-22T12:30:00Z"), shape: "circle", position: "aboveBar" });
  });

  it("formats the time axis in New York time, naming the day at day boundaries", () => {
    render(<SessionChart bars={bars} height={300} label="NQ chart" />);
    const scale = lw.options!.timeScale as { tickMarkFormatter: (t: number, type: number) => string };
    expect(scale.tickMarkFormatter(seconds("2026-10-22T12:30:00Z"), 3)).toBe("08:30");
    expect(scale.tickMarkFormatter(seconds("2026-10-22T00:00:00Z"), 2)).toBe("Wed 20:00");
    expect(scale.tickMarkFormatter(seconds("2026-01-15T13:30:00Z"), 3)).toBe("08:30"); // EST
  });

  it("shows a pin's card with its note link on hover, and keeps it open on click", () => {
    const status = vi.fn();
    render(<SessionChart bars={bars} pins={pins.pins} height={300} label="NQ chart" onStatus={status} />);
    act(() => lw.move!({ time: seconds("2026-10-22T12:30:00Z"), point: { x: 50, y: 20 } }));
    const card = screen.getByRole("tooltip");
    expect(card).toHaveTextContent("Jobless claims");
    expect(card).toHaveTextContent("Lower than forecast");
    expect(card).toHaveTextContent("NQ -0.06% in 15 min");
    expect(screen.getByRole("link", { name: "Read the note →" })).toHaveAttribute("href", "?note=release%3A20261022T1230%3AINITIAL_CLAIMS");
    expect(status).toHaveBeenLastCalledWith(expect.stringContaining("O 29,118.00 H 29,123.00 L 29,116.00 C 29,120.00 · delayed"));
    act(() => lw.click!());
    expect(screen.getByRole("dialog")).toHaveTextContent("Jobless claims");
    act(() => lw.move!({}));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("explains a level line when the crosshair is on it", () => {
    render(<SessionChart bars={bars} levels={levels.levels} height={300} label="NQ chart" />);
    act(() => lw.move!({ time: seconds("2026-10-22T12:40:00Z"), point: { x: 80, y: 42 } }));
    const card = screen.getByRole("tooltip");
    expect(card).toHaveTextContent("Overnight high · 29,220.00");
    expect(card).toHaveTextContent("Overnight high: what it usually means.");
    expect(card).toHaveTextContent("So far: the window is still running.");
    act(() => lw.move!({ time: seconds("2026-10-22T12:40:00Z"), point: { x: 80, y: 200 } }));
    expect(screen.queryByRole("tooltip")).toBeNull();
  });

  it("says a hovered gap has no quotes", () => {
    const status = vi.fn();
    render(<SessionChart bars={bars} height={300} label="NQ chart" onStatus={status} />);
    act(() => lw.move!({ time: seconds("2026-10-22T12:35:00Z"), point: { x: 60, y: 200 } }));
    expect(status).toHaveBeenLastCalledWith("Thu 08:35 ET · no quotes in this bar");
  });

  it("adds overlays on the left % scale and removes them again", () => {
    const overlay = { key: "ES", label: "S&P 500 futures", slot: 0 as const, bars: esBars };
    const { rerender } = render(<SessionChart bars={bars} overlays={[overlay]} height={300} label="NQ chart" />);
    expect(lw.lines).toHaveLength(1);
    expect(lw.lines[0].options).toMatchObject({ priceScaleId: "left", title: "S&P 500 futures" });
    const format = (lw.lines[0].options.priceFormat as { formatter: (v: number) => string }).formatter;
    expect(format(1.234)).toBe("+1.23%");
    expect(lw.chart.applyOptions).toHaveBeenCalledWith({ leftPriceScale: { visible: true } });
    rerender(<SessionChart bars={bars} overlays={[]} height={300} label="NQ chart" />);
    expect(lw.chart.removeSeries).toHaveBeenCalled();
    expect(lw.chart.applyOptions).toHaveBeenLastCalledWith({ leftPriceScale: { visible: false } });
  });

  it("says when a range has no stored quotes", () => {
    render(<SessionChart bars={{ ...bars, bars: [] }} height={300} label="NQ chart" />);
    expect(screen.getByRole("status")).toHaveTextContent("No stored quotes for this range yet.");
  });
});
