import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { resetExplainers } from "../../../web/src/explainers/library";
import { rejectionText } from "../../../web/src/watchlists/api";
import { badgeOf, WatchlistsPage } from "../../../web/src/watchlists/WatchlistsPage";
import { chartInstruments } from "../chartFixtures";

// The Watchlists page against a fake core: GET the list, the registry and the
// quotes; PUT validates symbols the way core/domain/workspace.py does.

const quote = (symbol: string, state: Record<string, unknown>) => ({
  symbol, state: {
    value: 100, change: 1, change_pct: 1, timestamp: "2026-10-22T15:00:00Z", status: "live", feed_status: "delayed",
    source: "cnbc", expected_delay_seconds: 600, market_closed: false, ...state,
  },
});

const quotes = { generated_at: "2026-10-22T15:00:00Z", count: 4, instruments: [
  quote("NQ", { value: 21012.5, change: 105.25, change_pct: 0.5 }),
  quote("ES", { value: 5800.25, change: -12.5, change_pct: -0.21 }),
  quote("US2Y", { value: 3.512, change: 0.021, change_pct: 0.6, status: "stale", feed_status: "stale", expected_delay_seconds: 0, market_closed: true }),
  quote("BTC", { value: 110250, change: 250, change_pct: 0.23, feed_status: "live", expected_delay_seconds: 0, source: "binance" }),
] };

const reply = (status: number, body: unknown) => Promise.resolve({ ok: status < 400, status, json: async () => body });

function serve({ registry = true }: { registry?: boolean } = {}) {
  const known = new Set(chartInstruments.instruments.map((i) => i.symbol));
  const store = { name: "Desk strip", symbols: ["NQ", "ES", "US2Y"], updated_at: null as string | null };
  const puts: Array<{ name: string; symbols: string[] }> = [];
  const fetcher = vi.fn((url: string, init?: RequestInit) => {
    if (url === "/api/v1/watchlist" && init?.method === "PUT") {
      const body = JSON.parse(String(init.body)) as { name: string; symbols: string[] };
      puts.push(body);
      const bad = body.symbols.find((s) => !known.has(s));
      if (bad) return reply(422, { detail: [{ type: "value_error", loc: ["body", "symbols"], msg: `Value error, unknown instrument: ${bad}` }] });
      Object.assign(store, body, { updated_at: "2026-10-22T15:05:00Z" });
      return reply(200, store);
    }
    if (url === "/api/v1/watchlist") return reply(200, { ...store });
    if (url === "/api/v1/chart/instruments") return registry ? reply(200, chartInstruments) : reply(503, { detail: "down" });
    if (url === "/api/v1/instruments") return reply(200, quotes);
    if (url === "/api/v1/explainers") return reply(200, { entries: [] });
    return reply(404, { detail: "not found" });
  });
  vi.stubGlobal("fetch", fetcher);
  return { store, puts };
}

const symbols = () => within(screen.getByRole("table")).getAllByRole("row").slice(1)
  .map((row) => row.querySelector("b")?.textContent);
const addSymbol = (symbol: string) => {
  fireEvent.change(screen.getByLabelText("Add an instrument"), { target: { value: symbol } });
  fireEvent.click(screen.getByRole("button", { name: "Add" }));
};

beforeEach(() => resetExplainers());

describe("Watchlists page", () => {
  it("shows the list with last, change, % and a feed badge per row", async () => {
    serve();
    render(<WatchlistsPage />);
    expect(await screen.findByRole("table", { name: "Desk strip instruments" })).toBeInTheDocument();
    expect(symbols()).toEqual(["NQ", "ES", "US2Y"]);
    const nq = screen.getByText("NQ").closest("tr")!;
    expect(await within(nq).findByText("21012.50")).toBeInTheDocument();
    expect(within(nq).getByText("+105.25")).toHaveClass("up");
    expect(within(nq).getByText("+0.50%")).toBeInTheDocument();
    expect(within(nq).getByText("Delayed")).toBeInTheDocument();
    const es = screen.getByText("ES").closest("tr")!;
    expect(within(es).getByText("-12.50")).toHaveClass("down");
    const us2y = screen.getByText("US2Y").closest("tr")!;
    expect(within(us2y).getByText("3.512")).toBeInTheDocument();
    expect(within(us2y).getByText("Closed")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("renames, adds, removes and reorders, then saves with one PUT", async () => {
    const { store, puts } = serve();
    render(<WatchlistsPage />);
    await screen.findByRole("table");
    await waitFor(() => expect(screen.getByText("Nasdaq 100 futures")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText("Watchlist name"), { target: { value: "  Rates and tech " } });
    addSymbol("btc");
    expect(symbols()).toEqual(["NQ", "ES", "US2Y", "BTC"]);
    expect(screen.getByLabelText("Add an instrument")).toHaveValue("");
    fireEvent.click(screen.getByRole("button", { name: "Remove ES" }));
    fireEvent.click(screen.getByRole("button", { name: "Move BTC up" }));
    fireEvent.click(screen.getByRole("button", { name: "Move NQ down" }));
    expect(symbols()).toEqual(["BTC", "NQ", "US2Y"]);
    expect(screen.getByRole("button", { name: "Move BTC up" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Move US2Y down" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/^Saved at/)).toBeInTheDocument();
    expect(puts).toEqual([{ name: "Rates and tech", symbols: ["BTC", "NQ", "US2Y"] }]);
    expect(store.symbols).toEqual(["BTC", "NQ", "US2Y"]);
    expect(screen.getByLabelText("Watchlist name")).toHaveValue("Rates and tech");
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("undoes unsaved changes and refuses a blank name or a repeat", async () => {
    const { puts } = serve();
    render(<WatchlistsPage />);
    await screen.findByRole("table");
    fireEvent.click(screen.getByRole("button", { name: "Remove NQ" }));
    fireEvent.click(screen.getByRole("button", { name: "Undo changes" }));
    expect(symbols()).toEqual(["NQ", "ES", "US2Y"]);
    addSymbol("ES");
    expect(screen.getByRole("alert")).toHaveTextContent("ES is already on the list.");
    fireEvent.change(screen.getByLabelText("Watchlist name"), { target: { value: "   " } });
    expect(screen.getByText("Give the list a name before saving.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    expect(puts).toEqual([]);
  });

  it("stops an unknown symbol at the add box when the registry is loaded", async () => {
    const { puts } = serve();
    render(<WatchlistsPage />);
    await waitFor(() => expect(screen.getByText("Nasdaq 100 futures")).toBeInTheDocument());
    addSymbol("FOO");
    expect(screen.getByRole("alert")).toHaveTextContent("FOO isn't an instrument Omarkets knows.");
    expect(symbols()).toEqual(["NQ", "ES", "US2Y"]);
    expect(puts).toEqual([]);
  });

  it("shows the core's 422 for a bad symbol and keeps the edit to fix", async () => {
    const { store, puts } = serve({ registry: false });
    render(<WatchlistsPage />);
    expect(await screen.findByText(/Instrument list unavailable/)).toBeInTheDocument();
    addSymbol("FOO");
    expect(symbols()).toEqual(["NQ", "ES", "US2Y", "FOO"]);
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't save the watchlist: unknown instrument: FOO");
    expect(puts).toHaveLength(1);
    expect(store.symbols).toEqual(["NQ", "ES", "US2Y"]);
    fireEvent.click(screen.getByRole("button", { name: "Remove FOO" }));
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled(); // back to the saved list
    addSymbol("BTC");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/^Saved at/)).toBeInTheDocument();
    expect(store.symbols).toEqual(["NQ", "ES", "US2Y", "BTC"]);
  });

  it("says when the watchlist cannot be read", async () => {
    vi.stubGlobal("fetch", vi.fn(() => reply(503, { detail: "workspace store unavailable" })));
    render(<WatchlistsPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Watchlist unavailable: workspace store unavailable");
  });
});

describe("watchlist helpers", () => {
  it("turns FastAPI 422 bodies into one sentence", () => {
    expect(rejectionText("unknown instrument: FOO")).toBe("unknown instrument: FOO");
    expect(rejectionText([{ loc: ["body", "symbols"], msg: "Value error, listed more than once: ES" }])).toBe("listed more than once: ES");
    expect(rejectionText([{ loc: ["body", "name"], msg: "String should have at least 1 character" }])).toBe("Name: String should have at least 1 character");
    expect(rejectionText({})).toBeNull();
  });

  it("picks the feed badge from the core's quote state", () => {
    expect(badgeOf(undefined)).toBe("none");
    expect(badgeOf(quote("NQ", {}) as never)).toBe("delayed");
    expect(badgeOf(quote("BTC", { feed_status: "live", expected_delay_seconds: 0 }) as never)).toBe("live");
    expect(badgeOf(quote("NQ", { status: "stale" }) as never)).toBe("stale");
    expect(badgeOf(quote("US2Y", { status: "stale", market_closed: true }) as never)).toBe("closed");
    expect(badgeOf(quote("NQ", { value: null }) as never)).toBe("none");
  });
});
