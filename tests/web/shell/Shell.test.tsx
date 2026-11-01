import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Root } from "../../../web/src/Root";
import { feedsStatus } from "../../../web/src/shell/feeds";
import { queueFocusSave } from "../../../web/src/shell/focus";
import { deskSchema } from "../../../web/src/today/api";
import { desk, draft, note, ok } from "../deskFixtures";
import { health } from "../fixtures";
import { focusOf } from "../chartFixtures";

// The canvas chart has its own test; jsdom cannot draw it.
vi.mock("../../../web/src/chart/SessionChart", () => ({ SessionChart: ({ label }: { label: string }) => <div role="img" aria-label={label} /> }));

afterEach(() => window.history.replaceState(null, "", "/"));

const older = { ...note, id: "move:1", kind: "move", title: "Big move", created_at: "2026-10-14T11:00:00Z",
  draft: { ...draft, headline: "Nasdaq slides as fear rises" } };

const row = (symbol: string, extra: Record<string, unknown>, state: Record<string, unknown>) => ({
  symbol, display_name: symbol, asset_class: "equity_index_future", quote_type: "price", timezone: "America/New_York",
  unit: "index points", venue: "CME", currency: "USD", session_calendar: "cme_globex",
  session_calendar_label: "CME Globex (Sun–Fri 18:00–17:00 ET)", price_precision: 2, stale_after_seconds: 60, metadata: {},
  ...extra, state: { symbol, status: "live", feed_status: "delayed", source: "cnbc", expected_delay_seconds: 600, market_closed: false, ...state },
});
const instruments = { generated_at: "2026-10-22T15:00:00Z", count: 3, instruments: [
  row("NQ", {}, {}),
  row("BTC", { asset_class: "crypto", venue: "BINANCE", session_calendar: "24x7", session_calendar_label: "24×7 (day = 00:00–24:00 UTC)" },
    { feed_status: "live", source: "binance", expected_delay_seconds: 0 }),
  row("US2Y", { asset_class: "treasury_yield", venue: "OTC", currency: null, price_precision: 3 },
    { status: "stale", feed_status: "stale", expected_delay_seconds: 0, market_closed: true }),
] };

// The global focus strip: NQ with the desk's own tiles.
const strip = { generated_at: null, focus: focusOf("NQ").instrument, watchlist_name: "Default",
  symbols: desk.quotes.map((q) => q.symbol), quotes: desk.quotes };

const stripFor = (symbol: string) => ({ ...strip, focus: focusOf(symbol).instrument });
// The core refuses Explain for a focus the desk does not write for (BTC here).
const refused = (detail: string) => Promise.resolve({ ok: false, status: 400, json: async () => ({ detail }) });

type Gates = { save?: Promise<unknown>; refetch?: Promise<unknown> };

// A fake core holding the saved focus: the strip reports it, a focus PUT
// changes it (after `gates.save`), and Explain writes for `?instrument=` or,
// with none, the saved focus. `asked` is what each Explain request wrote for.
function serve(body: unknown = desk, focus = "NQ", gates: Gates = {}) {
  let saved = focus;
  let strips = 0;
  const asked: string[] = [];
  const fetcher = vi.fn((url: string, init?: RequestInit) => {
    if (url === "/health") return ok(health);
    if (url === "/api/v1/focus/strip") {
      const reply = stripFor(saved);
      return strips++ > 0 && gates.refetch ? gates.refetch.then(() => ok(reply)) : ok(reply);
    }
    if (url === "/api/v1/chart/focus" && init?.method === "PUT") {
      const { symbol } = JSON.parse(String(init.body)) as { symbol: string };
      return (gates.save ?? Promise.resolve()).then(() => { saved = symbol; return ok(focusOf(symbol)); });
    }
    if (url === "/api/v1/instruments") return ok(instruments);
    if (url.startsWith("/api/v1/desk/ask")) {
      const written = new URL(url, "http://core").searchParams.get("instrument") ?? saved;
      asked.push(written);
      return written === "BTC" ? refused("The desk does not write for BTC") : ok({ ...note, id: "ask:1", kind: "ask" });
    }
    if (url === "/api/v1/desk/learn") return ok({ count: 0, entries: [] });
    return ok(body);
  });
  vi.stubGlobal("fetch", fetcher);
  return Object.assign(fetcher, { asked });
}

const asks = (fetcher: ReturnType<typeof serve>) =>
  fetcher.mock.calls.map(([url]) => url).filter((url) => url.startsWith("/api/v1/desk/ask"));
const gate = () => {
  let open!: () => void;
  const promise = new Promise<void>((resolve) => { open = resolve; });
  return { promise, open };
};

const current = () => screen.getByRole("link", { current: "page" });
const press = (key: string, target: Element | Window = window) => fireEvent.keyDown(target, { key });

describe("shared shell", () => {
  it("has one header with the wordmark, eight tabs, the feeds dot and Explain", async () => {
    serve();
    render(<Root />);
    const tabs = screen.getByRole("navigation", { name: "Pages" });
    expect([...tabs.querySelectorAll("a")].map((a) => a.textContent)).toEqual(["Today", "Chart", "Calendar", "Macro", "News", "Playbook", "Learn", "Watchlists"]);
    expect(screen.getByRole("link", { name: "Omarkets home" })).toBeInTheDocument();
    expect(await screen.findByText("All feeds OK")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Explain the market now (E)" })).toBeEnabled();
    expect(screen.queryByText("← Back to Today")).toBeNull();
  });

  it("switches pages from the tabs and keeps the URL in step", async () => {
    serve();
    render(<Root />);
    fireEvent.click(screen.getByRole("link", { name: "Calendar" }));
    expect(current()).toHaveTextContent("Calendar");
    expect(window.location.search).toBe("?view=calendar");
    expect(screen.getByRole("region", { name: "Economic calendar" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("link", { name: "Today" }));
    expect(window.location.search).toBe("");
  });

  it("opens old deep links on the new pages", async () => {
    serve();
    window.history.replaceState(null, "", "/?section=overview");
    render(<Root />);
    expect(current()).toHaveTextContent("Chart");
    expect(await screen.findByRole("region", { name: "NQ chart" })).toBeInTheDocument();
  });

  it("opens the system status panel from the feeds dot and closes it with Esc", async () => {
    serve();
    render(<Root />);
    const dot = await screen.findByRole("button", { name: "All feeds OK" });
    fireEvent.click(dot);
    const panel = screen.getByRole("dialog", { name: "System status" });
    expect(panel).toHaveTextContent("Database");
    expect(panel).toHaveTextContent("AI writer");
    expect(panel).toHaveTextContent("not endorsed or certified by the Federal Reserve Bank of St. Louis");
    expect(panel).toHaveTextContent("not endorsed or certified by the Census Bureau");
    expect(panel).toHaveTextContent("BLS.gov cannot vouch for the data");
    expect(dot).toHaveAttribute("aria-expanded", "true");
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "System status" })).toBeNull();
  });

  it("lists every instrument read-only in the status panel", async () => {
    serve();
    render(<Root />);
    fireEvent.click(await screen.findByRole("button", { name: "All feeds OK" }));
    const table = await screen.findByRole("table", { name: "Instruments" });
    const rows = [...table.querySelectorAll("tbody tr")].map((tr) => [...tr.querySelectorAll("td")].map((td) => td.textContent));
    expect(rows).toEqual([
      ["NQ", "equity index future", "CME", "USD", "CME Globex (Sun–Fri 18:00–17:00 ET)", "2", "cnbc · 10 min delay", "delayed"],
      ["BTC", "crypto", "BINANCE", "USD", "24×7 (day = 00:00–24:00 UTC)", "2", "binance · real time", "live"],
      ["US2Y", "treasury yield", "OTC", "—", "CME Globex (Sun–Fri 18:00–17:00 ET)", "3", "cnbc · real time", "market closed"],
    ]);
    expect(table.querySelector("input, select, button")).toBeNull();
    expect(screen.getByRole("dialog", { name: "System status" })).toHaveTextContent("As of Oct 22");
  });

  it("refreshes the Instruments table while the panel is open", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const fetcher = serve();
      render(<Root />);
      fireEvent.click(await screen.findByRole("button", { name: "All feeds OK" }));
      await screen.findByRole("table", { name: "Instruments" });
      const calls = () => fetcher.mock.calls.filter(([url]) => url === "/api/v1/instruments").length;
      const before = calls();
      await vi.advanceTimersByTimeAsync(15_000);
      expect(calls()).toBe(before + 1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("switches pages with 1–8 and ignores keys typed into inputs", async () => {
    serve();
    render(<Root />);
    press("2");
    expect(current()).toHaveTextContent("Chart");
    press("5");
    expect(await screen.findByRole("heading", { name: "News" })).toBeInTheDocument();
    press("8");
    expect(current()).toHaveTextContent("Watchlists");
    press("7");
    expect(current()).toHaveTextContent("Learn");
    const search = await screen.findByLabelText("Search lessons");
    press("1", search);
    press("e", search);
    expect(current()).toHaveTextContent("Learn");
    press("1", document.body);
    expect(current()).toHaveTextContent("Today");
  });

  it("ignores shortcuts pressed with Ctrl, Alt or Meta", () => {
    serve();
    render(<Root />);
    fireEvent.keyDown(window, { key: "2", ctrlKey: true });
    expect(current()).toHaveTextContent("Today");
  });

  it("E writes a note from any page and opens it on Today", async () => {
    const fetcher = serve();
    window.history.replaceState(null, "", "/?view=macro");
    render(<Root />);
    await screen.findByText("All feeds OK");
    press("e");
    await waitFor(() => expect(fetcher).toHaveBeenCalledWith("/api/v1/desk/ask", expect.objectContaining({ method: "POST" })));
    expect(current()).toHaveTextContent("Today");
  });

  it("Explain asks the core for its saved focus, not NQ", async () => {
    const fetcher = serve(desk, "ES");
    render(<Root />);
    await screen.findByRole("link", { name: /Focus: S&P 500/ });
    fireEvent.click(screen.getByRole("button", { name: "Explain the market now (E)" }));
    await waitFor(() => expect(asks(fetcher)).toEqual(["/api/v1/desk/ask"]));
    expect(fetcher.asked).toEqual(["ES"]);
  });

  it("Explain right after saving ES, before the strip has refetched, asks for ES", async () => {
    const refetch = gate();
    const fetcher = serve(desk, "NQ", { refetch: refetch.promise });
    render(<Root />);
    await screen.findByRole("link", { name: /Focus: Nasdaq/ });
    await act(() => queueFocusSave(focusOf("ES").instrument));
    // The save has landed but the strip still shows NQ.
    expect(screen.getByRole("link", { name: /Focus: Nasdaq/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Explain the market now (E)" }));
    await waitFor(() => expect(fetcher.asked).toEqual(["ES"]));
    expect(asks(fetcher)).toEqual(["/api/v1/desk/ask"]);
    await act(async () => refetch.open());
  });

  it("Explain while an ES save is still in flight waits for it, then asks for ES", async () => {
    const save = gate();
    const fetcher = serve(desk, "NQ", { save: save.promise });
    render(<Root />);
    await screen.findByRole("link", { name: /Focus: Nasdaq/ });
    let saving: Promise<unknown> = Promise.resolve();
    act(() => { saving = queueFocusSave(focusOf("ES").instrument); });
    fireEvent.click(screen.getByRole("button", { name: "Explain the market now (E)" }));
    await act(async () => { await Promise.resolve(); });
    expect(asks(fetcher)).toEqual([]);
    await act(async () => { save.open(); await saving; });
    await waitFor(() => expect(fetcher.asked).toEqual(["ES"]));
  });

  it("Explain on a focus the desk does not write for shows the refusal, with no NQ fallback", async () => {
    const fetcher = serve(desk, "BTC");
    render(<Root />);
    await screen.findByRole("link", { name: /Focus: Bitcoin/ });
    fireEvent.click(screen.getByRole("button", { name: "Explain the market now (E)" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The desk does not write for BTC");
    expect(asks(fetcher)).toEqual(["/api/v1/desk/ask"]);
    expect(fetcher.asked).toEqual(["BTC"]);
  });

  it("J and K move between notes on Today", async () => {
    serve({ ...desk, notes: [note, older] });
    render(<Root />);
    await screen.findByText("Inflation ran hotter than expected");
    const head = (text: string) => screen.getByText(text).closest("button")!;
    expect(head("Inflation ran hotter than expected")).toHaveAttribute("aria-expanded", "true");
    press("j");
    expect(head("Nasdaq slides as fear rises")).toHaveAttribute("aria-expanded", "true");
    expect(head("Inflation ran hotter than expected")).toHaveAttribute("aria-expanded", "false");
    expect(head("Nasdaq slides as fear rises")).toHaveFocus();
    press("j");
    expect(head("Nasdaq slides as fear rises")).toHaveAttribute("aria-expanded", "true");
    press("k");
    expect(head("Inflation ran hotter than expected")).toHaveAttribute("aria-expanded", "true");
  });

  it("opens the note named in ?note=", async () => {
    serve({ ...desk, notes: [note, older] });
    window.history.replaceState(null, "", "/?note=move%3A1");
    render(<Root />);
    const head = (await screen.findByText("Nasdaq slides as fear rises")).closest("button")!;
    expect(head).toHaveAttribute("aria-expanded", "true");
  });

  it("explains a quote tile with the tile's own data", async () => {
    serve();
    render(<Root />);
    const tile = (await screen.findByText("29,700.00")).closest("[role=button]")!;
    fireEvent.mouseEnter(tile);
    const card = screen.getByRole("tooltip", { name: "Nasdaq 100 futures" });
    expect(card).toHaveTextContent("Last: 29,700.00");
    expect(card).toHaveTextContent("Change since yesterday's close: -0.40%");
    expect(card).toHaveTextContent("Delayed quote");
    press("Escape");
    expect(screen.queryByRole("tooltip")).toBeNull();
  });
});

describe("feeds status", () => {
  const parsed = deskSchema.parse(desk);
  it("is pending before anything loads and down when the core is unreachable", () => {
    expect(feedsStatus({ desk: null, deskError: null, health: null, healthError: null }).tone).toBe("pending");
    expect(feedsStatus({ desk: null, deskError: "HTTP 503", health: null, healthError: "Backend unavailable" })).toMatchObject({ tone: "down", label: "Omarkets unavailable" });
  });
  it("reports all feeds OK when the core reports nothing wrong", () => {
    expect(feedsStatus({ desk: parsed, deskError: null, health, healthError: null })).toMatchObject({ tone: "ok", label: "All feeds OK" });
  });
  it("judges staleness on the focus strip's tiles when it has them", () => {
    const btc = { ...parsed.quotes[0], symbol: "BTC", label: "Bitcoin", stale: true, closed: false };
    expect(feedsStatus({ desk: parsed, deskError: null, health, healthError: null, quotes: [btc] }).lines.join(" "))
      .toMatch(/Stale quotes: Bitcoin/);
    expect(feedsStatus({ desk: parsed, deskError: null, health, healthError: null, quotes: [] })).toMatchObject({ tone: "ok" });
  });
  it("reports a focus that can't be read as an issue", () => {
    const status = feedsStatus({ desk: parsed, deskError: null, health, healthError: null, focusError: "HTTP 503" });
    expect(status).toMatchObject({ tone: "warn", label: "1 issue" });
    expect(status.lines[0]).toBe("Focus: HTTP 503 · showing NQ and the desk's tiles");
  });
  it("does not count closed-market quotes as issues, but still counts a failing provider", () => {
    const closed = { ...parsed, quotes: parsed.quotes.map((q) => ({ ...q, stale: false, closed: true, closed_text: "Markets closed" })) };
    expect(feedsStatus({ desk: closed, deskError: null, health, healthError: null })).toMatchObject({ tone: "ok" });
    const failing = feedsStatus({ desk: closed, deskError: null, healthError: null,
      health: { ...health, economic_providers: [{ name: "bls", status: "unavailable", checked_at: null, detail: "Provider check or persistence failed; cached data retained" }] } });
    expect(failing.label).toBe("1 issue");
    expect(failing.lines.join(" ")).toMatch(/Economic sources not healthy: bls \(unavailable\)/);
    expect(failing.lines.join(" ")).not.toMatch(/Stale quotes/);
  });
  it("counts stale quotes, provider trouble and source outages as issues", () => {
    const stale = { ...parsed, quotes: parsed.quotes.map((q) => ({ ...q, stale: true })) };
    const status = feedsStatus({ desk: stale, deskError: null, healthError: null,
      health: { ...health, provider_status: "degraded", news: { ...health.news!, offline_sources: 1 } } });
    expect(status.tone).toBe("warn");
    expect(status.label).toBe("3 issues");
    expect(status.lines.join(" ")).toMatch(/Stale quotes: Nasdaq 100 futures/);
    expect(feedsStatus({ desk: parsed, deskError: null, healthError: null, health: { ...health, database_ok: false } }).tone).toBe("down");
  });
});
