import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ChartPage } from "../../../web/src/chart/ChartPage";
import { ok } from "../deskFixtures";
import { bars, btcBars, chartInstruments, focusOf, levels, pins, watchlist } from "../chartFixtures";

// The Chart page with the real SessionChart (only lightweight-charts is faked),
// so a focus switch is tested down to the explain card and the footer readout.

type Handler = (param: { time?: number; point?: { x: number; y: number } }) => void;
const lw = vi.hoisted(() => ({ move: null as Handler | null, click: null as Handler | null }));
vi.mock("lightweight-charts", () => {
  const series = () => ({
    setData: () => undefined, applyOptions: () => undefined, attachPrimitive: () => undefined,
    createPriceLine: (o: unknown) => o, removePriceLine: () => undefined, priceToCoordinate: () => 300,
  });
  return {
    ColorType: { Solid: "solid" }, LineStyle: { Dashed: 2 },
    TickMarkType: { Year: 0, Month: 1, DayOfMonth: 2, Time: 3, TimeWithSeconds: 4 },
    CandlestickSeries: "Candlestick", LineSeries: "Line",
    createSeriesMarkers: () => ({ setMarkers: () => undefined }),
    createChart: () => ({
      applyOptions: () => undefined, remove: () => undefined, removeSeries: () => undefined,
      addSeries: series,
      subscribeCrosshairMove: (handler: Handler) => { lw.move = handler; },
      unsubscribeCrosshairMove: () => undefined,
      subscribeClick: (handler: Handler) => { lw.click = handler; },
      unsubscribeClick: () => undefined,
      timeScale: () => ({ fitContent: () => undefined, options: () => ({ barSpacing: 6 }), timeToCoordinate: () => 10 }),
    }),
  };
});

type Answer = () => Promise<unknown>;

/** A fake core. `hold` delays the PUT for one symbol until released. */
function serve(saved = "NQ") {
  const store = { symbol: saved };
  const held = new Map<string, () => void>();
  const holds = new Set<string>();
  const put = (symbol: string): Promise<unknown> => new Promise((resolve) => {
    const done = () => { store.symbol = symbol; resolve(ok(focusOf(symbol))); };
    if (holds.has(symbol)) held.set(symbol, done); else done();
  });
  const fetcher = vi.fn((url: string, init?: RequestInit): Promise<unknown> => {
    const answers: Array<[string, Answer]> = [
      ["/api/v1/chart/instruments", () => ok(chartInstruments)],
      ["/api/v1/watchlist", () => ok(watchlist)],
      ["/api/v1/bars/BTC", () => ok({ ...btcBars, interval: "15m", range: "5d" })],
      ["/api/v1/bars/NQ", () => ok(bars)],
      ["/api/v1/levels/NQ", () => ok(levels)],
      ["/api/v1/chart/pins", () => ok(pins)],
    ];
    if (url === "/api/v1/chart/focus" && init?.method === "PUT") return put(JSON.parse(String(init.body)).symbol);
    if (url === "/api/v1/chart/focus") return ok(focusOf(store.symbol));
    for (const [prefix, answer] of answers) if (url.startsWith(prefix)) return answer();
    return Promise.resolve({ ok: false, status: 404, json: async () => ({ detail: "not in this test" }) });
  });
  vi.stubGlobal("fetch", fetcher);
  return { store, hold: (symbol: string) => holds.add(symbol), release: (symbol: string) => held.get(symbol)?.() };
}

const seconds = (iso: string) => Date.parse(iso) / 1000;

describe("Chart focus switch", () => {
  it("drops NQ's pinned release card and OHLC readout when switching to BTC", async () => {
    serve();
    render(<ChartPage />);
    const picker = await screen.findByRole("combobox", { name: "Instrument" });
    await waitFor(() => expect(picker).not.toBeDisabled());
    await screen.findByRole("button", { name: /Jobless claims/ });
    const onPin = { time: seconds("2026-10-22T12:30:00Z"), point: { x: 50, y: 20 } };
    act(() => lw.move!(onPin));
    expect(screen.queryByRole("dialog")).toBeNull();
    act(() => lw.click!(onPin));
    expect(screen.getByRole("dialog")).toHaveTextContent("Jobless claims");
    expect(screen.getByText(/O 29,118\.00 H 29,123\.00/)).toBeInTheDocument();

    fireEvent.change(picker, { target: { value: "BTC" } });
    await screen.findByRole("region", { name: "BTC chart" });
    await screen.findByText("Source: binance");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByRole("tooltip")).toBeNull();
    expect(screen.queryByText(/Jobless claims/)).toBeNull();
    expect(screen.queryByText(/O 29,118\.00/)).toBeNull();
  });

  it("never lets a save from an earlier visit overwrite a newer choice", async () => {
    const core = serve("NQ");
    const first = render(<ChartPage />);
    let picker = await screen.findByRole("combobox", { name: "Instrument" });
    await waitFor(() => expect(picker).not.toBeDisabled());
    core.hold("ES");
    fireEvent.change(picker, { target: { value: "ES" } });
    fireEvent.change(picker, { target: { value: "BTC" } });
    first.unmount();

    // Reopen while the ES save is still in flight: the page shows the newest
    // choice (BTC), and a new pick queues behind the old saves.
    render(<ChartPage />);
    picker = await screen.findByRole("combobox", { name: "Instrument" });
    expect(picker).toHaveValue("BTC");
    await waitFor(() => expect(picker).not.toBeDisabled());
    fireEvent.change(picker, { target: { value: "US2Y" } });
    await act(async () => { core.release("ES"); });
    await waitFor(() => expect(core.store.symbol).toBe("US2Y"));
    expect(picker).toHaveValue("US2Y");
    expect(screen.getByRole("region", { name: "US2Y chart" })).toBeInTheDocument();
  });
});
