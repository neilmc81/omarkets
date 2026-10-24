import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { newsBoardSchema, type NewsBoard, type NewsItem } from "../../../web/src/newsboard/api";
import { NewsPage } from "../../../web/src/newsboard/NewsPage";
import { ok } from "../deskFixtures";

afterEach(() => vi.unstubAllGlobals());

const item = (over: Partial<NewsItem>): NewsItem => ({
  id: "a", at: "2026-10-22T12:30:00Z", source: "cnbc_top", source_label: "CNBC", headline: "Initial jobless claims fall to 194,000",
  url: null, category: "economy", category_label: "Economy", scheduled: true, nq_15m: -0.06,
  nq_15m_text: "NQ -0.06% in the next 15 min", nq_15m_status: "measured", alert: false, ...over,
});

const board: NewsBoard = {
  generated_at: "2026-10-22T16:00:00Z", start: "2026-10-21T16:00:00Z", hidden: 1,
  items: [
    item({ id: "b", headline: "Oil jumps on Middle East tensions", category: "energy", category_label: "Energy", scheduled: false, nq_15m: null, nq_15m_text: null, nq_15m_status: "waiting", alert: true }),
    item({ id: "c", headline: "Prince on a chat show", category: null, category_label: null, scheduled: false, nq_15m: null, nq_15m_text: null, nq_15m_status: "unmeasured" }),
    item({}),
  ],
  alerts: [{ id: "b", rule: "conflict", label: "Attack or invasion", headline: "Oil jumps on Middle East tensions", source: "CNBC", url: null, published_at: "2026-10-22T15:50:00Z", created_at: "2026-10-22T15:51:00Z" }],
};

describe("News page", () => {
  it("parses the board and rejects unknown categories", () => {
    expect(newsBoardSchema.parse(board).items).toHaveLength(3);
    expect(() => newsBoardSchema.parse({ ...board, items: [item({ category: "sport" as never })] })).toThrow();
  });

  it("shows market-moving headlines by default and the rest behind a count", async () => {
    vi.stubGlobal("fetch", vi.fn(() => ok(board)));
    render(<NewsPage />);
    const table = await screen.findByRole("table");
    expect(within(table).getByText("Initial jobless claims fall to 194,000")).toBeInTheDocument();
    expect(within(table).queryByText("Prince on a chat show")).toBeNull();
    const claims = within(table).getByText("Initial jobless claims fall to 194,000").closest("tr")!;
    expect(within(claims).getByText("Scheduled")).toBeInTheDocument();
    expect(within(claims).getByText("-0.06%")).toHaveClass("down");
    const oil = within(table).getByText("Oil jumps on Middle East tensions").closest("tr")!;
    expect(oil).toHaveClass("is-alert");
    expect(within(oil).getByText("measuring…")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Breaking alerts today" })).toHaveTextContent("Attack or invasion");
    expect(screen.getByText(/Hidden: 1 headline not related to markets/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show" }));
    expect(within(screen.getByRole("table")).getByText("Prince on a chat show")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Energy" }));
    expect(within(screen.getByRole("table")).queryByText("Initial jobless claims fall to 194,000")).toBeNull();
    expect(within(screen.getByRole("table")).getByText("Oil jumps on Middle East tensions")).toBeInTheDocument();
  });
});
