import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { resetExplainers } from "../../../web/src/explainers/library";
import { macroBoardSchema, type MacroBoard, type MacroCard } from "../../../web/src/macro/api";
import { MacroPage } from "../../../web/src/macro/MacroPage";
import { ok } from "../deskFixtures";

afterEach(() => { resetExplainers(); vi.unstubAllGlobals(); });

const card = (over: Partial<MacroCard>): MacroCard => ({
  key: "cpi", group: "inflation", title: "CPI year over year", subtitle: null, value: 3.7, value_text: "3.7%",
  period_text: "Sep 2026", at: null, trend: "rising", trend_text: "rising: the last 3 months averaged higher than the 3 before",
  nq_when_rising: "bad", nq_text: "A rise usually weighs on NQ; a fall usually helps it.", nq_effect: "bad",
  points: [3.0, 3.2, 3.7], release: {
    at: "2026-10-14T12:30:00Z", text: "Last release: above forecast", nq_effect: "bad",
    figure: { name: "CPI y/y", unit: "percent", actual: 3.7, forecast: 3.6, previous: null, decimals: 1, verdict: "above",
      actual_text: "3.7%", forecast_text: "3.6%", previous_text: "—", reading: "worse", reading_text: "Hotter than forecast" },
  }, explainer_key: null, sources: ["Source: BLS series CUUR0000SA0."], stale: false, quote_symbol: null, ...over,
});

const board: MacroBoard = {
  generated_at: "2026-10-23T15:00:00Z",
  groups: [
    { key: "inflation", title: "Inflation", cards: [card({}), card({ key: "pce", title: "PCE inflation year over year", value: null, value_text: "—", period_text: null, trend: "unknown", trend_text: "unavailable", nq_effect: "neutral", points: [], release: null, stale: true })] },
    { key: "jobs", title: "Jobs", cards: [card({ key: "payrolls", group: "jobs", title: "Payrolls monthly change", value_text: "+29K", nq_when_rising: "mixed", nq_text: "A rise or fall can cut both ways for NQ.", nq_effect: "mixed", trend: "falling", release: null })] },
    { key: "growth", title: "Growth", cards: [] },
    { key: "fed", title: "The Fed", cards: [card({ key: "fed_next", group: "fed", title: "Next Fed decision", value: null, value_text: "Wed Oct 28", period_text: "14:00 ET", at: "2026-10-28T18:00:00Z", trend: "unknown", trend_text: "", nq_effect: "neutral", points: [], release: null })] },
  ],
};

describe("Macro page", () => {
  it("parses the core's board and rejects unknown trends", () => {
    expect(macroBoardSchema.parse(board).groups).toHaveLength(4);
    expect(() => macroBoardSchema.parse({ ...board, groups: [{ ...board.groups[0], cards: [{ ...card({}), trend: "up" }] }] })).toThrow();
  });

  it("shows four groups, colours by NQ effect and keeps unavailable values visible", async () => {
    vi.stubGlobal("fetch", vi.fn((url: string) => ok(url === "/api/v1/desk/macro" ? board : url === "/api/v1/explainers" ? { entries: [] } : { generated_at: "x", interval: "15m", lines: [] })));
    render(<MacroPage />);
    const inflation = await screen.findByRole("region", { name: "Inflation" });
    const cpi = within(inflation).getByRole("article", { name: "CPI year over year" });
    expect(within(cpi).getByText("rising").closest("p")).toHaveClass("tone-down");
    expect(within(cpi).getByText("Last release: above forecast")).toHaveClass("worse");
    const pce = within(inflation).getByRole("article", { name: "PCE inflation year over year" });
    expect(pce).toHaveClass("is-stale");
    expect(within(pce).getByText("—")).toBeInTheDocument();
    expect(within(pce).getByText("unavailable", { selector: "span" })).toBeInTheDocument();
    const payrolls = screen.getByRole("article", { name: "Payrolls monthly change" });
    expect(within(payrolls).getByText("falling").closest("p")).toHaveClass("tone-flat");
    expect(screen.getByRole("article", { name: "Next Fed decision" })).toHaveTextContent("Wed Oct 28");
  });

  it("explains a value and its sources on click, not on focus", async () => {
    vi.stubGlobal("fetch", vi.fn((url: string) => ok(url === "/api/v1/desk/macro" ? board : url === "/api/v1/explainers" ? { entries: [] } : { generated_at: "x", interval: "15m", lines: [] })));
    render(<MacroPage />);
    const value = await screen.findByText("+29K");
    fireEvent.focus(value.closest(".explain-trigger")!);
    expect(screen.queryByRole("tooltip")).toBeNull();
    fireEvent.click(value.closest(".explain-trigger")!);
    await waitFor(() => expect(screen.getByRole("tooltip")).toHaveTextContent("cut both ways"));
    const [info] = screen.getAllByLabelText("Sources");
    fireEvent.click(info.closest(".explain-trigger")!);
    expect(screen.getAllByRole("tooltip")).toHaveLength(1);
    await waitFor(() => expect(screen.getByRole("tooltip")).toHaveTextContent("Source: BLS series"));
  });
});
