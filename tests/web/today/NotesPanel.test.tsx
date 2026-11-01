import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Root } from "../../../web/src/Root";
import { LAST_SEEN_KEY } from "../../../web/src/today/notesList";
import { desk, draft, note, ok } from "../deskFixtures";

// The canvas chart has its own test; jsdom cannot draw it.
vi.mock("../../../web/src/chart/SessionChart", () => ({ SessionChart: ({ label }: { label: string }) => <div role="img" aria-label={label} /> }));

// Thu Oct 22 2026, 14:00 New York (EDT) = 18:00 UTC.
const NOW = "2026-10-22T18:00:00Z";

const make = (id: string, created_at: string, headline: string, kind = "release", nq_tone = "neutral") =>
  ({ ...note, id, kind, created_at, draft: { ...draft, headline, nq_tone } });

const notes = [
  make("auction:1", "2026-10-22T17:27:00Z", "Weak demand at 5-year TIPS auction", "auction"),
  make("move:1", "2026-10-22T14:49:00Z", "Nasdaq slides further as fear rises", "move", "negative"),
  make("release:claims", "2026-10-22T13:02:00Z", "Jobless claims lower than expected"),
  make("premarket:wed", "2026-10-21T13:01:00Z", "Tech leads a quiet pre-market", "premarket", "supportive"),
  make("ask:wed", "2026-10-22T03:30:00Z", "Late Wednesday check-in", "ask"), // Wed 23:30 EDT
  make("primer:mon", "2026-10-19T11:10:00Z", "The week ahead", "primer"),
];

const list = () => screen.getByRole("region", { name: "Notes list" });

function start(stored: string | null, extra: (url: string) => unknown = () => undefined) {
  if (stored) window.localStorage.setItem(LAST_SEEN_KEY, stored);
  const fetcher = vi.fn((url: string) => ok(extra(url) ?? { ...desk, notes }));
  vi.stubGlobal("fetch", fetcher);
  render(<Root />);
  return fetcher;
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(NOW));
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.localStorage.clear();
  window.history.replaceState(null, "", "/");
});

describe("Today notes list", () => {
  it("opens the newest note and lists the rest of today as compact rows", async () => {
    start(null);
    expect(await screen.findByText("What it means")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Weak demand at 5-year TIPS auction" })).toBeInTheDocument();
    const row = screen.getByRole("button", { name: /Nasdaq slides further/ });
    expect(row).toHaveTextContent("10:49 ET");
    expect(row).toHaveTextContent("Big move");
    expect(within(row).getByText("Negative")).toHaveAttribute("title", "Negative for NQ");
    expect(screen.getByRole("button", { name: /Jobless claims lower/ })).toHaveTextContent("09:02 ET");
  });

  it("titles each row's tone with the instrument it is about", async () => {
    const es = { ...make("move:ES:1", "2026-10-22T14:30:00Z", "S&P futures hold up", "move", "mixed"),
      instrument: "ES", tone_for: "ES" };
    start(null, () => ({ ...desk, notes: [...notes, es] }));
    const row = await screen.findByRole("button", { name: /S&P futures hold up/ });
    expect(within(row).getByText("Mixed")).toHaveAttribute("title", "Mixed for ES");
  });

  it("folds Yesterday and Earlier behind a count, split at New York midnight", async () => {
    start(null);
    await screen.findByText("What it means");
    const yesterday = screen.getByRole("button", { name: /Yesterday › 2 notes/ });
    expect(yesterday).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("Late Wednesday check-in")).not.toBeInTheDocument();
    fireEvent.click(yesterday);
    expect(screen.getByRole("button", { name: /Late Wednesday check-in/ })).toHaveTextContent("23:30 ET");
    expect(screen.getByRole("button", { name: /Tech leads a quiet pre-market/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Earlier › 1 note/ }));
    expect(screen.getByRole("button", { name: /The week ahead/ })).toHaveTextContent("Mon, Oct 19 · 07:10 ET");
  });

  it("expands a row on click and folds the previously open note into a row", async () => {
    start(null);
    await screen.findByText("What it means");
    fireEvent.click(screen.getByRole("button", { name: /Nasdaq slides further/ }));
    expect(screen.getByRole("heading", { name: "Nasdaq slides further as fear rises" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Weak demand at 5-year TIPS auction/ })).toHaveAttribute("aria-expanded", "false");
    expect(screen.getAllByText("What it means")).toHaveLength(1);
  });

  it("shows no divider on a first visit and records the visit", async () => {
    start(null);
    await screen.findByText("What it means");
    expect(screen.queryByText("New since you last looked")).not.toBeInTheDocument();
    expect(window.localStorage.getItem(LAST_SEEN_KEY)).toBe("2026-10-22T18:00:00.000Z");
  });

  it("puts the divider above notes written since the last visit and holds it while reading", async () => {
    start("2026-10-22T14:00:00Z");
    await screen.findByText("What it means");
    const items = within(list()).getAllByRole("listitem");
    expect(items[0]).toHaveTextContent("New since you last looked");
    expect(items[1]).toHaveTextContent("Weak demand at 5-year TIPS auction");
    expect(screen.getByRole("button", { name: /Nasdaq slides further/ })).toHaveClass("is-new");
    expect(screen.getByRole("button", { name: /Jobless claims lower/ })).not.toHaveClass("is-new");
    // The visit is stored at once, but the divider keeps the old time until the next visit.
    expect(window.localStorage.getItem(LAST_SEEN_KEY)).toBe("2026-10-22T18:00:00.000Z");
    fireEvent.click(screen.getByRole("button", { name: /Jobless claims lower/ }));
    expect(screen.getByText("New since you last looked")).toBeInTheDocument();
  });

  it("starts a new visit when the window comes back into view", async () => {
    start("2026-10-22T14:00:00Z");
    await screen.findByText("What it means");
    const visibility = vi.spyOn(document, "visibilityState", "get");
    visibility.mockReturnValue("hidden");
    act(() => { document.dispatchEvent(new Event("visibilitychange")); });
    visibility.mockReturnValue("visible");
    act(() => { document.dispatchEvent(new Event("visibilitychange")); });
    expect(screen.queryByText("New since you last looked")).not.toBeInTheDocument();
  });

  it("moves through notes with J and K, opening folded days as needed", async () => {
    start(null);
    await screen.findByText("What it means");
    for (let i = 0; i < 3; i += 1) fireEvent.keyDown(window, { key: "j" });
    expect(screen.getByRole("heading", { name: "Late Wednesday check-in" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Yesterday › 2 notes/ })).toHaveAttribute("aria-expanded", "true");
    fireEvent.keyDown(window, { key: "k" });
    expect(screen.getByRole("heading", { name: "Jobless claims lower than expected" })).toBeInTheDocument();
  });

  it("opens ?note= for a note in the desk list", async () => {
    window.history.replaceState(null, "", "/?note=premarket%3Awed");
    start(null);
    expect(await screen.findByRole("heading", { name: "Tech leads a quiet pre-market" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Weak demand at 5-year TIPS auction/ })).toBeInTheDocument();
  });

  it("fetches a linked note that is older than the desk's recent list", async () => {
    window.history.replaceState(null, "", "/?note=release%3Aold");
    const old = make("release:old", "2026-09-17T12:51:00Z", "CPI hotter than expected back in September");
    const fetcher = start(null, (url) => (url === "/api/v1/desk/notes/release%3Aold" ? old : undefined));
    expect(await screen.findByRole("heading", { name: "CPI hotter than expected back in September" })).toBeInTheDocument();
    expect(fetcher).toHaveBeenCalledWith("/api/v1/desk/notes/release%3Aold", expect.anything());
    expect(screen.getByRole("button", { name: /Earlier › 2 notes/ })).toHaveAttribute("aria-expanded", "true");
  });

  it("says so when a linked note cannot be loaded", async () => {
    window.history.replaceState(null, "", "/?note=release%3Agone");
    vi.stubGlobal("fetch", vi.fn((url: string) => url.startsWith("/api/v1/desk/notes/")
      ? Promise.resolve({ ok: false, status: 404, json: async () => ({ detail: "Note not found" }) })
      : ok({ ...desk, notes })));
    render(<Root />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not open that note: Note not found");
  });
});
