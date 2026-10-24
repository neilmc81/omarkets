import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Root } from "../../../web/src/Root";
import { feedsStatus } from "../../../web/src/shell/feeds";
import { deskSchema } from "../../../web/src/today/api";
import { desk, draft, note, ok } from "../deskFixtures";
import { health } from "../fixtures";

// The canvas chart has its own test; jsdom cannot draw it.
vi.mock("../../../web/src/chart/SessionChart", () => ({ SessionChart: ({ label }: { label: string }) => <div role="img" aria-label={label} /> }));

afterEach(() => window.history.replaceState(null, "", "/"));

const older = { ...note, id: "move:1", kind: "move", title: "Big move", created_at: "2026-10-14T11:00:00Z",
  draft: { ...draft, headline: "Nasdaq slides as fear rises" } };

function serve(body: unknown = desk) {
  const fetcher = vi.fn((url: string) => url === "/health" ? ok(health)
    : url === "/api/v1/desk/ask" ? ok({ ...note, id: "ask:1", kind: "ask" })
      : url === "/api/v1/desk/learn" ? ok({ count: 0, entries: [] })
        : ok(body));
  vi.stubGlobal("fetch", fetcher);
  return fetcher;
}

const current = () => screen.getByRole("link", { current: "page" });
const press = (key: string, target: Element | Window = window) => fireEvent.keyDown(target, { key });

describe("shared shell", () => {
  it("has one header with the wordmark, seven tabs, the feeds dot and Explain", async () => {
    serve();
    render(<Root />);
    const tabs = screen.getByRole("navigation", { name: "Pages" });
    expect([...tabs.querySelectorAll("a")].map((a) => a.textContent)).toEqual(["Today", "Chart", "Calendar", "Macro", "News", "Playbook", "Learn"]);
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
    expect(screen.getByRole("region", { name: "NQ chart" })).toBeInTheDocument();
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

  it("switches pages with 1–7 and ignores keys typed into inputs", async () => {
    serve();
    render(<Root />);
    press("2");
    expect(current()).toHaveTextContent("Chart");
    press("5");
    expect(await screen.findByRole("heading", { name: "News" })).toBeInTheDocument();
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
