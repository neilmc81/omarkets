import { render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ScorecardPage } from "../../../web/src/scorecard/ScorecardPage";
import { ok } from "../deskFixtures";

afterEach(() => vi.unstubAllGlobals());

const group = (kind: string, prompt_version: string, written: number, extra = {}) => ({
  kind, prompt_version, written, failed: 1, why: null, verdicts: null,
  ratings: { useful: 2, not_useful: 1, unrated: written - 3 }, ...extra,
});

const period = (start: string, end: string, groups: unknown[], costs: unknown[]) => ({
  start, end, written: 9, failed: 2, by_kind: [], by_prompt_version: [], groups, costs,
});

const card = {
  generated_at: "2026-10-28T12:00:00Z",
  current: period("2026-10-21T12:00:00Z", "2026-10-28T12:00:00Z", [
    group("move", "desk:v11", 5, {
      why: { reported: 1, likely: 2, unknown: 1, not_recorded: 1 },
      verdicts: { grew: 1, held: 2, faded: 0, reversed: 1, quiet: 0, no_verdict: 1 } }),
    group("move", "desk:v9", 4, { why: { reported: 0, likely: 0, unknown: 0, not_recorded: 4 } }),
  ], [
    { model: "deepseek/x", prompt_version: "desk:v11", notes: 6, priced: 5, unpriced: 1, total_usd: 0.05, mean_usd: 0.01 },
    { model: "unknown", prompt_version: "unknown", notes: 1, priced: 0, unpriced: 1, total_usd: 0, mean_usd: null },
  ]),
  previous: period("2026-10-14T12:00:00Z", "2026-10-21T12:00:00Z", [], []),
};

describe("Scorecard page", () => {
  it("renders exactly what the endpoint returns, split by prompt version", async () => {
    const fetcher = vi.fn(() => ok(card));
    vi.stubGlobal("fetch", fetcher);
    render(<ScorecardPage />);
    const current = await screen.findByRole("region", { name: "Last 7 days" });
    expect(fetcher).toHaveBeenCalledWith("/api/v1/desk/scorecard", expect.anything());
    expect(current).toHaveTextContent("Oct 21 – Oct 28 (UTC) · 9 written, 2 failed");
    const rows = within(current).getAllByRole("row").map((r) => r.textContent);
    expect(rows.some((r) => r?.startsWith("Big movedesk:v1151") && r.includes("1 / 2 / 1 / 1") && r.includes("1 / 2 / 0 / 1 / 0 / 1"))).toBe(true);
    expect(rows.some((r) => r?.startsWith("Big movedesk:v941") && r.includes("0 / 0 / 0 / 4"))).toBe(true);
    expect(within(current).getAllByText("—").length).toBeGreaterThan(0); // no follow-up verdicts for v9
  });

  it("says not available for a mean with no cost and leaves an empty period empty", async () => {
    vi.stubGlobal("fetch", vi.fn(() => ok(card)));
    render(<ScorecardPage />);
    const current = await screen.findByRole("region", { name: "Last 7 days" });
    expect(within(current).getByText("not available")).toBeInTheDocument();
    expect(within(current).getByText("6 (1 without a cost)")).toBeInTheDocument();
    expect(within(current).getByText("$0.0100")).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "The 7 days before" })).getByText("No notes in this period.")).toBeInTheDocument();
  });

  it("shows an error when the core is down", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response("{}", { status: 503 }))));
    render(<ScorecardPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Scorecard unavailable");
  });
});
