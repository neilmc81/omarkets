import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LearnPage } from "../../../web/src/learn/LearnPage";
import { ok } from "../deskFixtures";

afterEach(() => vi.unstubAllGlobals());

const entry = (text: string, topic: string, label: string, heading: string | null, times = 1) => ({
  text, note_id: text.slice(0, 6), kind: "ask", title: "Market now", created_at: "2026-10-22T13:00:00Z",
  times, topic, topic_label: label, heading,
});

const learn = {
  count: 2, taught: 6,
  entries: [entry("Bid-to-cover: demand at an auction.", "auctions", "Auctions", "Bid-to-cover"),
    entry("A basis point (bp) is 0.01%.", "yields", "Bonds & yields", "Basis point (bp)", 5)],
  topics: [{ key: "auctions", label: "Auctions", count: 1 }, { key: "yields", label: "Bonds & yields", count: 1 }],
  most_taught: [entry("A basis point (bp) is 0.01%.", "yields", "Bonds & yields", "Basis point (bp)", 5)],
};

describe("Learn page", () => {
  it("filters by the core's topics and shows progress", async () => {
    vi.stubGlobal("fetch", vi.fn(() => ok(learn)));
    render(<LearnPage />);
    expect(await screen.findByRole("heading", { name: "Bid-to-cover" })).toBeInTheDocument();
    expect(screen.getByText("taught 5 times")).toBeInTheDocument();
    const progress = screen.getByRole("complementary", { name: "Your progress" });
    expect(progress).toHaveTextContent("2 lessons · 2 topics");
    expect(progress).toHaveTextContent("Basis point (bp)5×");
    fireEvent.click(screen.getByRole("button", { name: "Auctions" }));
    expect(screen.queryByText("A basis point (bp) is 0.01%.")).toBeNull();
    fireEvent.change(screen.getByLabelText("Search lessons"), { target: { value: "fed" } });
    expect(screen.getByText(/No lesson matches "fed" in this topic/)).toBeInTheDocument();
  });
});
