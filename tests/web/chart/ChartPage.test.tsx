import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ChartPage } from "../../../web/src/chart/ChartPage";
import type { Overlay } from "../../../web/src/chart/SessionChart";
import { ok } from "../deskFixtures";
import type { Bars, KeyLevel, Pin } from "../../../web/src/chart/api";
import { bars, btcBars, chartInstruments, esBars, esLevels, focusOf, levels, pins, watchlist } from "../chartFixtures";

type Drawn = { bars: Bars | null; overlays?: Overlay[]; levels?: KeyLevel[]; pins?: Pin[]; label: string; precision?: number };
const drawn = vi.hoisted(() => ({ props: null as null | Drawn, all: [] as Drawn[] }));
vi.mock("../../../web/src/chart/SessionChart", () => ({
  SessionChart: (props: Drawn) => {
    drawn.props = props;
    drawn.all.push(props);
    return <div role="img" aria-label={props.label} />;
  },
}));

function serve(over: Record<string, () => Promise<unknown>> = {}, saved = "NQ") {
  const fetcher = vi.fn((url: string, init?: RequestInit) => {
    for (const [prefix, answer] of Object.entries(over)) if (url.startsWith(prefix)) return answer();
    if (url === "/api/v1/chart/focus" && init?.method === "PUT") {
      return ok(focusOf(JSON.parse(String(init.body)).symbol));
    }
    if (url === "/api/v1/chart/focus") return ok(focusOf(saved));
    if (url === "/api/v1/chart/instruments") return ok(chartInstruments);
    if (url === "/api/v1/watchlist") return ok(watchlist);
    if (url.startsWith("/api/v1/bars/BTC")) return ok({ ...btcBars, interval: "15m", range: "5d" });
    if (url.startsWith("/api/v1/levels/ES")) return ok(esLevels);
    if (url.startsWith("/api/v1/bars/ES")) return ok(esBars);
    if (url.startsWith("/api/v1/bars/")) return ok({ ...bars, interval: url.includes("15m") ? "15m" : "5m", range: url.includes("range=1m") ? "1m" : url.includes("5d") ? "5d" : "1d" });
    if (url.startsWith("/api/v1/levels/")) return ok(levels);
    if (url.startsWith("/api/v1/chart/pins")) return ok(pins);
    return Promise.reject(new Error(`unexpected ${url}`));
  });
  vi.stubGlobal("fetch", fetcher);
  return fetcher;
}

describe("Chart page", () => {
  it("draws NQ with every price level and the pins, and lists both for keyboard users", async () => {
    serve();
    render(<ChartPage />);
    await waitFor(() => expect(drawn.props?.pins).toHaveLength(2));
    expect(screen.getByRole("img", { name: /NQ candles, 5 sessions, 15m bars/ })).toBeInTheDocument();
    expect(drawn.props?.levels).toHaveLength(7); // all but the gap
    const pinLink = screen.getByRole("button", { name: /Thu 08:30 ET · Jobless claims/ });
    fireEvent.click(pinLink);
    expect(within(screen.getByRole("dialog")).getByRole("link", { name: "Read the note →" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Charts by TradingView" })).toBeInTheDocument();
    expect(screen.getByText("Delayed about 10 minutes")).toBeInTheDocument();
  });

  it("fetches an overlay only when it is switched on", async () => {
    const fetcher = serve();
    render(<ChartPage />);
    await waitFor(() => expect(drawn.props).not.toBeNull());
    expect(fetcher.mock.calls.some(([url]) => String(url).startsWith("/api/v1/bars/ES"))).toBe(false);
    fireEvent.click(screen.getByRole("checkbox", { name: "S&P 500 futures" }));
    await waitFor(() => expect(drawn.props?.overlays?.map((o) => o.key)).toEqual(["ES"]));
    expect(fetcher).toHaveBeenCalledWith("/api/v1/bars/ES?interval=15m&range=5d", expect.anything());
    fireEvent.click(screen.getByRole("checkbox", { name: "S&P 500 futures" }));
    await waitFor(() => expect(drawn.props?.overlays).toEqual([]));
  });

  it("offers 15-minute and hourly bars only for one month", async () => {
    const fetcher = serve();
    render(<ChartPage />);
    fireEvent.change(await screen.findByRole("combobox", { name: "Bar size" }), { target: { value: "5m" } });
    fireEvent.click(screen.getByRole("button", { name: "1M" }));
    await waitFor(() => expect(fetcher).toHaveBeenCalledWith("/api/v1/bars/NQ?interval=15m&range=1m", expect.anything()));
    const options = within(screen.getByRole("combobox", { name: "Bar size" })).getAllByRole("option").map((o) => o.textContent);
    expect(options).toEqual(["15m", "1h"]);
  });

  it("shows stale and unavailable data plainly", async () => {
    serve({
      "/api/v1/bars/NQ": () => ok({ ...bars, status: "stale" }),
      "/api/v1/levels/": () => Promise.resolve({ ok: false, status: 503, json: async () => ({ detail: "storage unavailable" }) }),
    });
    render(<ChartPage />);
    expect(await screen.findByText(/Stale: no fresh NQ quote recently/)).toBeInTheDocument();
    expect(await screen.findByText("unavailable (storage unavailable)")).toBeInTheDocument();
  });

  it("cycles NQ → ES → BTC → NQ without ever mixing instruments, saving each choice in order", async () => {
    const fetcher = serve();
    drawn.all = [];
    render(<ChartPage />);
    await waitFor(() => expect(drawn.props?.pins).toHaveLength(2));
    const picker = screen.getByRole("combobox", { name: "Instrument" });
    await waitFor(() => expect(picker).not.toBeDisabled());
    expect(within(picker).getAllByRole("group").map((g) => g.getAttribute("label"))).toEqual(["Desk strip", "Other instruments"]);
    const expect_ = { NQ: { levels: 7, pins: 2, precision: 2 }, ES: { levels: 7, pins: 0, precision: 2 }, BTC: { levels: 0, pins: 0, precision: 2 } };
    for (const symbol of ["ES", "BTC", "NQ"] as const) {
      fireEvent.change(picker, { target: { value: symbol } });
      await waitFor(() => expect(drawn.props?.bars?.symbol).toBe(symbol));
      await waitFor(() => expect(drawn.props?.levels).toHaveLength(expect_[symbol].levels));
      await waitFor(() => expect(drawn.props?.pins).toHaveLength(expect_[symbol].pins));
      expect(drawn.props?.precision).toBe(expect_[symbol].precision);
      expect(screen.getByRole("img", { name: new RegExp(`^${symbol} candles`) })).toBeInTheDocument();
      expect(screen.getByRole("region", { name: `${symbol} chart` })).toBeInTheDocument();
    }
    // Every frame drew one instrument: its bars, its own levels, pins only on NQ.
    for (const frame of drawn.all) {
      const symbol = frame.bars?.symbol;
      if (!symbol) continue;
      expect(frame.label.startsWith(`${symbol} candles`)).toBe(true);
      if (symbol !== "NQ") expect(frame.pins).toEqual([]);
      if (symbol === "BTC") expect(frame.levels).toEqual([]);
      if (symbol === "ES" && frame.levels?.length) expect(frame.levels).toEqual(esLevels.levels.filter((l) => l.key !== "gap_vs_settle"));
    }
    const puts = fetcher.mock.calls.filter(([, init]) => (init as RequestInit | undefined)?.method === "PUT");
    await waitFor(() => expect(puts.map(([, init]) => JSON.parse(String((init as RequestInit).body)).symbol)).toEqual(["ES", "BTC", "NQ"]));
    expect(puts.every(([url]) => url === "/api/v1/chart/focus")).toBe(true);
    // Nothing on this page asks the desk to write.
    expect(fetcher.mock.calls.some(([url]) => String(url).startsWith("/api/v1/desk"))).toBe(false);
  });

  it("opens on the saved focus and never fetches NQ first", async () => {
    const fetcher = serve({}, "BTC");
    render(<ChartPage />);
    await waitFor(() => expect(drawn.props?.bars?.symbol).toBe("BTC"));
    expect(screen.getByText("none for Bitcoin")).toBeInTheDocument();
    const urls = fetcher.mock.calls.map(([url]) => String(url));
    expect(urls.some((u) => u.startsWith("/api/v1/bars/NQ") || u.startsWith("/api/v1/levels/") || u.startsWith("/api/v1/chart/pins"))).toBe(false);
  });

  it("hides the overlay of the instrument in focus", async () => {
    serve({}, "ES");
    render(<ChartPage />);
    await waitFor(() => expect(drawn.props?.bars?.symbol).toBe("ES"));
    expect(screen.queryByRole("checkbox", { name: "S&P 500 futures" })).toBeNull();
    expect(screen.getByRole("checkbox", { name: "2-year yield" })).toBeInTheDocument();
  });

  it("falls back to NQ when the focus can't be read, and says when a choice can't be saved", async () => {
    serve({ "/api/v1/chart/focus": () => Promise.resolve({ ok: false, status: 503, json: async () => ({ detail: "workspace store unavailable" }) }) });
    render(<ChartPage />);
    await waitFor(() => expect(drawn.props?.bars?.symbol).toBe("NQ"));
    expect(screen.getByText("Chart focus unavailable (workspace store unavailable) · showing NQ")).toBeInTheDocument();
    const picker = screen.getByRole("combobox", { name: "Instrument" });
    await waitFor(() => expect(picker).not.toBeDisabled());
    fireEvent.change(picker, { target: { value: "ES" } });
    await waitFor(() => expect(drawn.props?.bars?.symbol).toBe("ES"));
    expect(await screen.findByText("Couldn't save the chart focus (workspace store unavailable)")).toBeInTheDocument();
  });
});
