import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { calendarBoardSchema, type CalendarBoard, type CalendarRow } from "../../../web/src/calendar/api";
import { CalendarPage, countdown, zoneOffset } from "../../../web/src/calendar/CalendarPage";
import { resetExplainers } from "../../../web/src/explainers/library";
import { ok } from "../deskFixtures";


afterEach(() => resetExplainers());

const figure = (over: Record<string, unknown> = {}) => ({
  name: "Unemployment Claims", unit: "count", actual: 194_000, forecast: 205_000, previous: 199_000, decimals: 0,
  verdict: "below", actual_text: "194K", forecast_text: "205K", previous_text: "199K",
  surprise: { value: -11_000, value_text: "−11K", size: "bigger", history_count: 8, larger_than: 7,
    text: "Bigger than usual surprise below the forecast — larger than 7 of the last 8" },
  reading: "better", reading_text: "Stronger jobs data than forecast", ...over,
});

const claims: CalendarRow = {
  at: "2026-10-22T12:30:00Z", title: "Jobless claims", codes: ["INITIAL_CLAIMS"], importance: "high",
  kind: "economic", released: true, figures: [figure()] as CalendarRow["figures"], venue: null,
  nq_15m: -0.06, nq_15m_text: "NQ -0.06% in 15 min", nq_15m_status: "measured",
  note_id: "release:20261022T1230:INITIAL_CLAIMS", explainer_key: "indicator:INITIAL_CLAIMS",
  explainer: "Short claims text.", playbook_text: "Jobless claims, first 15 min: Below forecast (3): NQ fell 2, rose 1.",
  playbook: null,
};

// 14:00 ET Thursday: 03:00 Friday in Tokyo.
const fed: CalendarRow = {
  ...claims, at: "2026-10-22T18:00:00Z", title: "Fed rate decision", codes: ["FOMC"], kind: "fed", released: false,
  figures: [figure({ name: "Federal Funds Rate", unit: "percent", actual: null, actual_text: "—", forecast: 4.25,
    forecast_text: "4.25%", previous_text: "4.50%", verdict: "not_reported", surprise: null, reading: null, reading_text: null })] as CalendarRow["figures"],
  nq_15m: null, nq_15m_text: null, nq_15m_status: "upcoming", note_id: null, explainer_key: "indicator:FOMC",
  explainer: "The Fed sets rates.", playbook_text: null,
};

const board: CalendarBoard = {
  generated_at: "2026-10-22T13:00:00Z", week: "this", start: "2026-10-19T04:00:00Z", end: "2026-10-26T04:00:00Z",
  tz: "Asia/Tokyo", all: false, hidden: 4, stale: false, coverage: ["Consensus history is partial."],
  days: [
    { date: "2026-10-22", label: "Thursday, Oct 22", rows: [claims] },
    { date: "2026-10-23", label: "Friday, Oct 23", rows: [fed] },
  ],
};

const claimsEntry = {
  key: "indicator:INITIAL_CLAIMS", kind: "indicator", name: "INITIAL_CLAIMS", title: "Jobless claims",
  what: "New claims for unemployment benefits last week.", why_nq: "A quick read on layoffs.",
  rising: "More layoffs; rate cuts more likely.", falling: "Fewer layoffs; a firm job market.",
  rising_label: "Above forecast", falling_label: "Below forecast",
};

function serve(body: CalendarBoard = board) {
  const fetcher = vi.fn((url: string) => {
    if (url.startsWith("/api/v1/explainers")) return ok({ entries: [claimsEntry] });
    if (url.startsWith("/api/v1/desk/calendar")) {
      const all = new URL(url, "http://x").searchParams.get("all") === "true";
      return ok({ ...body, all, hidden: all ? 0 : body.hidden });
    }
    return Promise.reject(new Error(`unexpected ${url}`));
  });
  vi.stubGlobal("fetch", fetcher);
  return fetcher;
}

describe("calendar schema", () => {
  it("accepts the core's board and keeps missing figures unavailable", () => {
    const parsed = calendarBoardSchema.parse(board);
    expect(parsed.days[1].rows[0].figures[0].actual).toBeNull();
    expect(() => calendarBoardSchema.parse({ ...board, days: [{ ...board.days[0], rows: [{ ...claims, nq_15m_status: "soon" }] }] })).toThrow();
  });

  it("formats countdowns and zone offsets for display", () => {
    const now = Date.parse("2026-10-22T13:00:00Z");
    expect(countdown("2026-10-22T13:12:00Z", now)).toBe("in 12m");
    expect(countdown("2026-10-22T18:05:00Z", now)).toBe("in 5h 05m");
    expect(countdown("2026-10-28T03:00:00Z", now)).toBe("in 5d 14h");
    expect(zoneOffset("Asia/Dubai")).toBe("UTC+4");
  });
});

describe("Calendar page", () => {
  it("groups by your day and shows your time first, New York second", async () => {
    const fetcher = serve();
    render(<CalendarPage timeZone="Asia/Tokyo" />);
    expect(await screen.findByText("Thursday, Oct 22")).toBeInTheDocument();
    expect(fetcher).toHaveBeenCalledWith("/api/v1/desk/calendar?week=this&all=false&tz=Asia%2FTokyo", expect.anything());
    const table = screen.getByRole("table");
    const groups = within(table).getAllByRole("rowgroup").slice(1);
    expect(within(groups[0]).getByText("Unemployment Claims")).toBeInTheDocument();
    expect(within(groups[1]).getByText("Friday, Oct 23")).toBeInTheDocument();
    const fedRow = within(groups[1]).getByText("Federal Funds Rate").closest("tr")!;
    expect(fedRow.querySelector(".cal-time")?.textContent).toBe("03:0014:00");
    expect(screen.getByText(/Times in your time zone \(UTC\+9\)/)).toBeInTheDocument();
    const claimsRow = within(groups[0]).getByText("Unemployment Claims").closest("tr")!;
    expect(within(claimsRow).getByText("194K")).toBeInTheDocument();
    expect(within(claimsRow).getByText("-0.06%")).toHaveClass("down");
    expect(within(claimsRow).getByRole("link", { name: "Read note" })).toHaveAttribute("href", "?note=release%3A20261022T1230%3AINITIAL_CLAIMS");
    expect(within(fedRow).getByText("upcoming")).toBeInTheDocument();
    expect(within(fedRow).getAllByText("—").length).toBeGreaterThan(0);
  });

  it("filters to market-moving releases by default and shows all on request", async () => {
    const fetcher = serve();
    render(<CalendarPage timeZone="Asia/Tokyo" />);
    expect(await screen.findByText("4 low-importance hidden")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: "Market-moving only" }));
    await waitFor(() => expect(fetcher).toHaveBeenCalledWith("/api/v1/desk/calendar?week=this&all=true&tz=Asia%2FTokyo", expect.anything()));
    await waitFor(() => expect(screen.queryByText("4 low-importance hidden")).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: "Next week" }));
    await waitFor(() => expect(fetcher).toHaveBeenCalledWith("/api/v1/desk/calendar?week=next&all=true&tz=Asia%2FTokyo", expect.anything()));
  });

  it("explains a release from the library and the surprise on click", async () => {
    serve();
    render(<CalendarPage timeZone="Asia/Tokyo" />);
    const event = await screen.findByRole("button", { name: "Unemployment Claims" });
    fireEvent.mouseEnter(event.parentElement!);
    expect(screen.queryByRole("dialog", { name: "Unemployment Claims" })).toBeNull();
    fireEvent.click(event);
    await waitFor(() => expect(screen.getByRole("dialog", { name: "Unemployment Claims" })).toHaveTextContent("New claims for unemployment benefits last week."));
    const card = screen.getByRole("dialog", { name: "Unemployment Claims" });
    expect(card).toHaveTextContent("Below forecast: Fewer layoffs; a firm job market.");
    expect(within(card).getByRole("link", { name: "Read the note →" })).toBeInTheDocument();
    const chip = screen.getByText(/−11K/);
    fireEvent.click(chip.closest("[role=button]")!);
    expect(screen.queryByRole("dialog", { name: "Unemployment Claims" })).toBeNull();
    const tip = screen.getByRole("tooltip", { name: "Surprise · Unemployment Claims" });
    expect(tip).toHaveTextContent("Stronger jobs data than forecast (better for the economy).");
    expect(tip).toHaveTextContent("larger than 7 of the last 8");
  });

  it("opens the playbook side panel for the next release and from What to watch", async () => {
    serve();
    render(<CalendarPage timeZone="Asia/Tokyo" />);
    const panel = await screen.findByRole("complementary", { name: "Playbook: Fed rate decision" });
    expect(panel).toHaveTextContent("Federal Funds Rate: forecast 4.25%");
    expect(panel).toHaveTextContent("Fed events have no forecast to beat");
    fireEvent.click(screen.getByRole("button", { name: "Close the playbook panel" }));
    expect(screen.queryByRole("complementary")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "What to watch" }));
    expect(screen.getByRole("complementary", { name: "Playbook: Fed rate decision" })).toBeInTheDocument();
    fireEvent.click(screen.getByText("194K"));
    expect(screen.getByRole("complementary", { name: "Playbook: Jobless claims" })).toHaveTextContent("Below forecast (3): NQ fell 2, rose 1.");
  });

  it("keeps caveats behind an ⓘ and says when the calendar is unavailable", async () => {
    serve({ ...board, stale: true });
    render(<CalendarPage timeZone="Asia/Tokyo" />);
    expect(screen.queryByText("Consensus history is partial.")).toBeNull();
    fireEvent.click(await screen.findByText("Calendar stale ⓘ"));
    expect(screen.getByRole("tooltip", { name: "About these figures" })).toHaveTextContent("Consensus history is partial.");
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ ok: false, status: 503, json: async () => ({ detail: "Desk is not enabled" }) })));
    render(<CalendarPage timeZone="UTC" />);
    expect(await screen.findByText(/Calendar unavailable · Desk is not enabled/)).toBeInTheDocument();
  });

  it("gives each figure of a shared release time its own line", async () => {
    const gdp = figure({ name: "Advance GDP q/q", unit: "percent", actual: 3.4, forecast: 3.2, previous: 1.5,
      actual_text: "3.4%", forecast_text: "3.2%", previous_text: "1.5%", verdict: "above", surprise: null,
      revised_previous_text: "2.2%", revision_text: "revised up by 0.7 percentage points" });
    const morning: CalendarRow = { ...claims, title: "Jobless claims & GDP", figures: [figure(), gdp] as CalendarRow["figures"] };
    serve({ ...board, days: [{ date: "2026-10-22", label: "Thursday, Oct 22", rows: [morning] }] });
    render(<CalendarPage timeZone="Asia/Tokyo" />);
    const gdpLine = (await screen.findByText("Advance GDP q/q")).closest("tr")!;
    const claimsLine = screen.getByText("Unemployment Claims").closest("tr")!;
    expect(gdpLine).not.toBe(claimsLine);
    expect(gdpLine).toHaveClass("cal-sub");
    expect(within(gdpLine).getByText("3.4%")).toHaveClass("up");
    expect(within(gdpLine).getByText("→ 2.2%", { exact: false })).toBeInTheDocument();
    expect(claimsLine.querySelector(".cal-time")).toHaveAttribute("rowspan", "2");
    expect(gdpLine.querySelector(".cal-time")).toBeNull();
    expect(screen.queryByText("Jobless claims & GDP")).toBeNull();
  });
});
