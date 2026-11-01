import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Root } from "../../../web/src/Root";
import { bars, btcBars, calendar, chartInstruments, esBars, esLevels, focusOf, levels, pins, watchlist } from "../chartFixtures";
import { desk, ok } from "../deskFixtures";

// Global focus: Today's market section and the header follow the focus the
// Chart page saves; the desk (notes, writer) never hears about it.

vi.mock("../../../web/src/chart/SessionChart", () => ({ SessionChart: ({ label }: { label: string }) => <div role="img" aria-label={label} /> }));

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); window.history.replaceState(null, "", "/"); });

const PRICE: Record<string, [string, string]> = {
  NQ: ["Nasdaq 100 futures", "29,200.00"], ES: ["S&P 500 futures", "6,805.00"], BTC: ["Bitcoin", "$110,250.00"],
  US10Y: ["10-year yield", "4.120%"], US2Y: ["2-year yield", "3.550%"], DXY: ["US dollar", "99.10"],
  VIX: ["VIX", "17.20"], WTI: ["Crude oil", "$61.40"], GOLD: ["Gold", "$4,010.00"],
};
const quote = (symbol: string) => ({
  symbol, label: PRICE[symbol][0], value: 1, value_text: PRICE[symbol][1], change_text: "+0.10%", direction: "up",
  as_of: "2026-10-22T13:59:00Z", delayed: false, stale: false, closed: false, closed_text: null, explainer: null,
});
const stripFor = (symbol: string) => {
  const symbols = [symbol, ...watchlist.symbols.filter((s) => s !== symbol)].slice(0, 8);
  return { generated_at: "2026-10-22T14:00:00Z", focus: focusOf(symbol).instrument, watchlist_name: watchlist.name,
    symbols, quotes: symbols.map(quote) };
};

function serve(start = "NQ") {
  const store = { symbol: start, stripDown: false };
  const fetcher = vi.fn((url: string, init?: RequestInit): Promise<unknown> => {
    if (url === "/api/v1/chart/focus" && init?.method === "PUT") {
      store.symbol = JSON.parse(String(init.body)).symbol;
      return ok(focusOf(store.symbol));
    }
    const answers: Array<[string, () => unknown]> = [
      ["/api/v1/session", () => calendar],
      ["/api/v1/chart/focus", () => focusOf(store.symbol)],
      ["/api/v1/chart/instruments", () => chartInstruments],
      ["/api/v1/watchlist", () => watchlist],
      ["/api/v1/bars/NQ", () => bars],
      ["/api/v1/bars/ES", () => esBars],
      ["/api/v1/bars/BTC", () => btcBars],
      ["/api/v1/levels/NQ", () => levels],
      ["/api/v1/levels/ES", () => esLevels],
      ["/api/v1/chart/pins", () => pins],
      ["/api/v1/desk", () => desk],
    ];
    const path = url.split("?")[0];
    if (path === "/api/v1/focus/strip") return store.stripDown
      ? Promise.resolve({ ok: false, status: 503, json: async () => ({ detail: "workspace store unavailable" }) })
      : ok(stripFor(store.symbol));
    for (const [route, answer] of answers) if (path === route) return ok(answer());
    return Promise.resolve({ ok: false, status: 404, json: async () => ({ detail: "not in this test" }) });
  });
  vi.stubGlobal("fetch", fetcher);
  return { store, fetcher };
}

const urls = (fetcher: ReturnType<typeof vi.fn>) => fetcher.mock.calls.map(([url]) => String(url));

async function pickOnChart(symbol: string) {
  fireEvent.click(screen.getByRole("link", { name: "Chart" }));
  const picker = await screen.findByRole("combobox", { name: "Instrument" });
  await waitFor(() => expect(picker).not.toBeDisabled());
  fireEvent.change(picker, { target: { value: symbol } });
  fireEvent.click(screen.getByRole("link", { name: "Today" }));
}

async function expectToday(symbol: string) {
  const [label, value] = PRICE[symbol];
  const chip = await screen.findByRole("link", { name: `Focus: ${label}` });
  expect(chip).toHaveTextContent(`${symbol}${value}`);
  const chart = await screen.findByRole("region", { name: `${symbol} session chart` });
  expect(within(chart).getByRole("heading")).toHaveTextContent(`${symbol} · this session`);
  // The first tile is the focus; no other focus's chart is on the page.
  const tiles = within(screen.getByRole("region", { name: "Markets" })).getAllByText(/./, { selector: ".t-quote-label" });
  expect(tiles[0]).toHaveTextContent(label);
  expect(tiles.filter((t) => t.textContent === label)).toHaveLength(1);
  for (const other of ["NQ", "ES", "BTC"].filter((s) => s !== symbol)) {
    expect(screen.queryByRole("region", { name: `${other} session chart` })).toBeNull();
  }
  return chart;
}

describe("Today follows the global focus", () => {
  it("starts on NQ with the desk strip's order", async () => {
    serve();
    render(<Root />);
    await expectToday("NQ");
    const tiles = within(screen.getByRole("region", { name: "Markets" })).getAllByText(/./, { selector: ".t-quote-label" });
    expect(tiles.map((t) => t.textContent)).toEqual(watchlist.symbols.map((s) => PRICE[s][0]));
    expect(await screen.findByText(/^NQZ26 · from delayed quotes · click/)).toBeInTheDocument();
  });

  it("NQ -> ES -> BTC -> NQ moves the strip, chart, levels and header together, never touching the desk", async () => {
    const { fetcher } = serve();
    render(<Root />);
    await expectToday("NQ");

    await pickOnChart("ES");
    await expectToday("ES");
    expect(await screen.findByText(/^ESZ26 · from delayed quotes · click/)).toBeInTheDocument();

    await pickOnChart("BTC");
    await expectToday("BTC");
    expect(screen.getByText(/None for Bitcoin/)).toBeInTheDocument();
    expect(urls(fetcher)).not.toContain("/api/v1/levels/BTC");

    await pickOnChart("NQ");
    await expectToday("NQ");
    expect(await screen.findByText(/^NQZ26 · from delayed quotes · click/)).toBeInTheDocument();

    // Sparklines follow the strip's own symbols.
    expect(urls(fetcher)).toContain(`/api/v1/sparklines?symbols=${stripFor("BTC").symbols.join(",")}`);
    // The desk is only ever read, never asked to write.
    const deskCalls = fetcher.mock.calls.filter(([url]) => String(url).startsWith("/api/v1/desk"));
    expect(deskCalls.length).toBeGreaterThan(0);
    expect(deskCalls.every(([url, init]) => url === "/api/v1/desk" && (!init?.method || init.method === "GET"))).toBe(true);
    expect(fetcher.mock.calls.filter(([, init]) => init?.method && init.method !== "GET").every(([url]) => url === "/api/v1/chart/focus")).toBe(true);
  });

  it("stays on NQ with the desk's tiles when the focus can't be read", async () => {
    const { fetcher } = serve();
    const answer = fetcher.getMockImplementation()!;
    fetcher.mockImplementation((url: string, init?: RequestInit) => url === "/api/v1/focus/strip"
      ? Promise.resolve({ ok: false, status: 503, json: async () => ({ detail: "workspace store unavailable" }) })
      : answer(url, init));
    render(<Root />);
    await screen.findByRole("region", { name: "NQ session chart" });
    expect(await screen.findByText("29,700.00")).toBeInTheDocument(); // the desk's NQ tile
    expect(screen.queryByRole("link", { name: /^Focus:/ })).toBeNull();
  });

  it("a focus without release pins never asks for them and shows none on the timeline", async () => {
    const { fetcher } = serve("BTC");
    render(<Root />);
    await expectToday("BTC");
    const timeline = await screen.findByRole("region", { name: "Session timeline" });
    expect(within(timeline).queryByText("Jobless claims")).toBeNull();
    expect(urls(fetcher).filter((u) => u.startsWith("/api/v1/chart/pins"))).toEqual([]);
  });

  it("NQ's timeline carries the release pins", async () => {
    serve();
    render(<Root />);
    await expectToday("NQ");
    const timeline = await screen.findByRole("region", { name: "Session timeline" });
    expect(await within(timeline).findByText("Jobless claims")).toBeInTheDocument();
  });

  it("drops a saved focus when a later read fails, then picks it up again", async () => {
    const { store } = serve("ES");
    render(<Root />);
    await expectToday("ES");

    // The strip breaks; the next read (after a save) must not keep ES on screen.
    store.stripDown = true;
    await pickOnChart("BTC");
    await screen.findByRole("region", { name: "NQ session chart" });
    expect(screen.queryByRole("region", { name: "ES session chart" })).toBeNull();
    expect(screen.queryByRole("region", { name: "BTC session chart" })).toBeNull();
    expect(screen.queryByRole("link", { name: /^Focus:/ })).toBeNull();
    expect(await screen.findByText("29,700.00")).toBeInTheDocument(); // the desk's NQ tile

    store.stripDown = false;
    await pickOnChart("ES");
    await expectToday("ES");
  });
});
