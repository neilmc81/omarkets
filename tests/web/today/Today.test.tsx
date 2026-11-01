import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Root } from "../../../web/src/Root";
import { etTime } from "../../../web/src/today/Today";
import { desk, note, ok } from "../deskFixtures";

// The workstation pages open sockets; Today's tests only need the shell.
// The canvas chart has its own test; jsdom cannot draw it.
vi.mock("../../../web/src/chart/SessionChart", () => ({ SessionChart: ({ label }: { label: string }) => <div role="img" aria-label={label} /> }));

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); window.history.replaceState(null, "", "/"); });

describe("Today", () => {
  it("shows the latest note expanded with figures, reaction, rates and coming up", async () => {
    vi.stubGlobal("fetch", vi.fn(() => ok(desk)));
    render(<Root />);
    expect(await screen.findByText("Inflation ran hotter than expected")).toBeInTheDocument();
    expect(screen.getByText("Above forecast")).toBeInTheDocument();
    expect(screen.getByText(/larger than 9 of the last 10/)).toBeInTheDocument();
    expect(screen.getByText("-0.40%", { selector: "b" })).toBeInTheDocument();
    expect(screen.getByText("The 2-year yield jumped 6bp.")).toBeInTheDocument();
    expect(screen.getByText("Negative for NQ")).toBeInTheDocument();
    expect(screen.getByText("Meta earnings")).toBeInTheDocument();
    expect(screen.getByText(/about 10 minutes delayed/)).toBeInTheDocument();
  });

  it("labels each note's tone with the instrument it is about", async () => {
    const es = { ...note, id: "move:ES:1", kind: "move", instrument: "ES", tone_for: "ES",
      draft: { ...note.draft, headline: "S&P futures slide", nq_tone: "supportive" } };
    vi.stubGlobal("fetch", vi.fn(() => ok({ ...desk, notes: [es] })));
    render(<Root />);
    expect(await screen.findByText("Supportive for ES")).toBeInTheDocument();
    expect(screen.queryByText(/for NQ/, { selector: ".t-tone" })).not.toBeInTheDocument();
  });

  it("names no instrument when an older core sends none", async () => {
    const old: Record<string, unknown> = { ...note };
    delete old.tone_for;
    delete old.instrument;
    vi.stubGlobal("fetch", vi.fn(() => ok({ ...desk, notes: [old] })));
    render(<Root />);
    expect(await screen.findByText("Negative", { selector: ".t-tone" })).toBeInTheDocument();
    expect(screen.queryByText(/for NQ/, { selector: ".t-tone" })).not.toBeInTheDocument();
  });

  it("collapses a note and asks for an on-demand explanation", async () => {
    const fetcher = vi.fn((url: string) => ok(url === "/api/v1/desk/ask" ? { ...note, id: "ask:1", kind: "ask" } : desk));
    vi.stubGlobal("fetch", fetcher);
    render(<Root />);
    const headline = await screen.findByText("Inflation ran hotter than expected");
    fireEvent.click(headline);
    expect(screen.queryByText("What it means")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Explain the market now (E)" }));
    await waitFor(() => expect(fetcher).toHaveBeenCalledWith("/api/v1/desk/ask", expect.objectContaining({ method: "POST" })));
  });

  it("shows a clear error when the backend is down, with no Details view left", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ ok: false, status: 503, json: async () => ({ detail: "Desk is not enabled" }) })));
    render(<Root />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Desk is not enabled");
    expect(screen.getByText("Omarkets unavailable")).toBeInTheDocument();
    expect(screen.queryByText("Details")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Explain the market now (E)" })).toBeDisabled();
  });

  it("labels New York time correctly on both sides of a daylight-saving change", () => {
    expect(etTime("2026-10-30T13:00:00Z")).toBe("Fri 09:00 ET"); // EDT
    expect(etTime("2026-11-02T14:00:00Z")).toBe("Mon 09:00 ET"); // EST
    expect(etTime("2027-03-15T13:00:00Z")).toBe("Mon 09:00 ET"); // EDT again
  });

  it("opens the Learn library from the tabs and searches it", async () => {
    const learn = { count: 2, entries: [
      { text: "CPI measures consumer prices.", note_id: "release:a", kind: "release",
        title: "Consumer prices (CPI)", created_at: "2026-10-14T12:51:00Z", times: 3 },
      { text: "The 2-year yield tracks Fed expectations.", note_id: "premarket:b", kind: "premarket",
        title: "Pre-market", created_at: "2026-10-13T13:01:00Z", times: 1 },
    ] };
    vi.stubGlobal("fetch", vi.fn((url: string) => ok(url === "/api/v1/desk/learn" ? learn : desk)));
    render(<Root />);
    await screen.findByText("Inflation ran hotter than expected");
    fireEvent.click(screen.getByRole("link", { name: "Learn" }));
    expect(await screen.findByText("CPI measures consumer prices.")).toBeInTheDocument();
    expect(screen.getByText(/taught 3 times/)).toBeInTheDocument();
    expect(screen.queryByText("Inflation ran hotter than expected")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Search lessons"), { target: { value: "fed yield" } });
    expect(screen.queryByText("CPI measures consumer prices.")).not.toBeInTheDocument();
    expect(screen.getByText("The 2-year yield tracks Fed expectations.")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Search lessons"), { target: { value: "dollar" } });
    expect(screen.getByText(/No lesson matches/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("link", { name: "Today" }));
    expect(await screen.findByText("Inflation ran hotter than expected")).toBeInTheDocument();
  });

  it("opens the Playbook and expands an indicator's releases", async () => {
    const move = (text: string, change: number) => ({ symbol: "NQ", label: "NQ", change, change_unit: "%",
      start_at: "2026-10-14T12:30:00Z", end_at: "2026-10-14T12:45:00Z", delayed: true, text });
    const playbook = { generated_at: "2026-10-23T00:00:00Z", since: "2026-10-14T12:30:00Z", min_releases: 5,
      indicators: [{ name: "Core CPI m/m", count: 1, enough: false,
        text: "Only 1 release measured so far: too few to call it a pattern (that needs at least 5). Treat these as examples.",
        buckets: [{ verdict: "above", count: 1, text: "Above forecast once. First 15 min: NQ fell 1. By the close: fell 1." }],
        releases: [{ at: "2026-10-14T12:30:00Z", title: "Core CPI", alongside: ["CPI y/y"],
          figure: { name: "Core CPI m/m", unit: "percent", actual: 0.3, forecast: 0.2, previous: 0.3, decimals: 1,
            verdict: "above", actual_text: "0.3%", forecast_text: "0.2%", previous_text: "0.3%" },
          nq_15m: move("-0.85%", -0.85), us2y_15m: { ...move("+15.0bp", 15), change_unit: "bp" },
          nq_close: move("-3.18%", -3.18), close_pending: false, held: "grew",
          note_id: "release:20261014T1230:CPI", surprise: 0.1, surprise_text: "+0.1 pt" }] }] };
    vi.stubGlobal("fetch", vi.fn((url: string) => ok(url === "/api/v1/desk/playbook" ? playbook : desk)));
    render(<Root />);
    await screen.findByText("Inflation ran hotter than expected");
    fireEvent.click(screen.getByRole("link", { name: "Playbook" }));
    // The first indicator opens straight away, with its tiles, plot and rows.
    expect(await screen.findByRole("heading", { name: "Core CPI m/m · 1 release" })).toBeInTheDocument();
    expect(screen.getByText(/too few to call it a pattern/)).toBeInTheDocument();
    expect(screen.getByText("-3.18%")).toBeInTheDocument();
    expect(screen.getByText("grew")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /Surprise against NQ's 15-minute move for 1 release/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Core CPI m\/m · 1/ })).toHaveAttribute("aria-pressed", "true");
    // The release row links to its note; following it opens Today with the note.
    fireEvent.click(screen.getByRole("link", { name: "Read note" }));
    expect(window.location.search).toBe("?note=release%3A20261014T1230%3ACPI");
    expect(await screen.findByText("What it means")).toBeInTheDocument();
  });

  it("shows a what-to-watch card with forecasts, playbook history and rules of thumb", async () => {
    const watch = [{ key: "20261022T1230:INITIAL_CLAIMS", at: "2026-10-22T12:30:00Z", title: "Jobless claims",
      importance: "high", explainer: "Weekly count of new unemployment filings.", released: false,
      figures: [{ name: "Unemployment Claims", forecast_text: "200K", previous_text: "194K", actual_text: null,
        verdict: null, history: ["Below forecast 2 times. First 15 min: NQ barely moved 2."],
        history_note: "Only 3 earlier releases: examples, not a pattern." }],
      readings: [{ family: "jobless", label: "Jobless figures", above: "Weaker job market.", below: "Tight job market." }] }];
    vi.stubGlobal("fetch", vi.fn(() => ok({ ...desk, watch })));
    render(<Root />);
    const card = await screen.findByRole("region", { name: "What to watch: Jobless claims" });
    expect(card).toHaveTextContent("200K");
    expect(card).toHaveTextContent("Below forecast 2 times");
    expect(card).toHaveTextContent("examples, not a pattern");
    expect(card).toHaveTextContent("If above forecast: Weaker job market.");
  });

  it("shows breaking alerts above the quotes", async () => {
    const alerts = [{ id: "e1", rule: "tariffs", label: "New tariffs",
      headline: "Trump imposes 50% tariffs on EU imports", source: "CNBC", url: "https://example.test/e1",
      published_at: "2026-10-14T12:55:00Z", created_at: "2026-10-14T12:56:00Z" }];
    vi.stubGlobal("fetch", vi.fn(() => ok({ ...desk, alerts })));
    render(<Root />);
    expect(await screen.findByRole("region", { name: "Breaking news" })).toHaveTextContent("New tariffs");
    expect(screen.getByRole("link", { name: "Trump imposes 50% tariffs on EU imports" })).toHaveAttribute("href", "https://example.test/e1");
  });

  it("shows the did-it-hold line on the open note's card", async () => {
    const followed = { ...note, follow_up: { checkpoints: [], verdict: "reversed", final: true,
      text: "First 15 min: NQ −0.40% · by the close: +0.60% — the first reaction reversed." } };
    vi.stubGlobal("fetch", vi.fn(() => ok({ ...desk, notes: [followed] })));
    render(<Root />);
    await screen.findByText("Inflation ran hotter than expected");
    expect(screen.getByText(/the first reaction reversed/)).toBeInTheDocument();
    expect(screen.getByText("Did it hold?")).toBeInTheDocument();
  });
});

describe("Today market section (redesign Phase 2)", () => {
  it("shows the timeline, tiles, risk tone, chart, next event and key levels from the core", async () => {
    const { bars, calendar, levels, pins, risk, sparklines } = await import("../chartFixtures");
    const next = { at: "2026-10-29T12:30:00Z", title: "PCE + GDP", importance: "critical", kind: "economic",
      explainer: null, forecast_text: null, playbook_text: "No earlier releases measured yet." };
    const routes: Array<[string, unknown]> = [
      ["/api/v1/session", calendar], ["/api/v1/levels/", levels], ["/api/v1/risk-tone", risk],
      ["/api/v1/sparklines", sparklines], ["/api/v1/chart/pins", pins], ["/api/v1/bars/", bars],
    ];
    vi.stubGlobal("fetch", vi.fn((url: string) => ok(routes.find(([prefix]) => url.startsWith(prefix))?.[1] ?? { ...desk, next_event: next })));
    render(<Root />);
    expect(await screen.findByRole("region", { name: "Session timeline" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /Risk tone: mixed/ })).toBeInTheDocument();
    expect(await screen.findByText("PCE + GDP")).toBeInTheDocument();
    expect(await screen.findByText("Overnight high (so far)")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /NQ candles, this session, 5-min bars/ })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Notes list" })).toBeInTheDocument();
  });
});
