import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { levelLines } from "../../../web/src/chart/explain";
import { explainerLibrarySchema, explainerLines, fetchExplainers, resetExplainers, type Explainer } from "../../../web/src/explainers/library";
import { KeyLevelsCard, QuoteStrip, quoteLines } from "../../../web/src/today/Market";
import type { Quote } from "../../../web/src/today/api";
import { levels } from "../chartFixtures";

const nq: Explainer = {
  key: "instrument:NQ", kind: "instrument", name: "NQ", title: "Nasdaq 100 futures",
  what: "Futures on the Nasdaq 100.", why_nq: "This is the market the desk follows.",
  rising: "Buyers are paying up for big tech.", falling: "Sellers are in charge for now.",
  rising_label: "Rising", falling_label: "Falling",
};
const overnightHigh: Explainer = {
  key: "level:overnight_high", kind: "level", name: "overnight_high", title: "Overnight high",
  what: "The highest price since 18:00 ET.", why_nq: "Traders often watch it.",
  rising: "A break above is usually read as strength.", falling: null,
  rising_label: "Above it", falling_label: null,
};
const cash: Explainer = {
  key: "segment:cash", kind: "segment", name: "cash", title: "US cash", what: "The US stock market is open.",
  why_nq: "Most of the volume.", rising: null, falling: null, rising_label: null, falling_label: null,
};

const quote: Quote = {
  symbol: "NQ", label: "Nasdaq 100 futures", value: 29_173.75, value_text: "29,173.75", change_text: "+0.11%",
  direction: "up", as_of: "2026-10-22T13:40:00Z", delayed: false, stale: false, closed: false, closed_text: null,
  explainer: "Short form from the quote payload.",
};

function serve(entries: Explainer[] = [nq, overnightHigh, cash]) {
  const fetcher = vi.fn(async () => new Response(JSON.stringify({ entries }), { status: 200 }));
  vi.stubGlobal("fetch", fetcher);
  return fetcher;
}

afterEach(() => { vi.unstubAllGlobals(); resetExplainers(); });

describe("explainer library schema", () => {
  it("parses the core's payload and rejects an unknown kind", () => {
    expect(explainerLibrarySchema.parse({ entries: [nq, cash] }).entries).toHaveLength(2);
    expect(() => explainerLibrarySchema.parse({ entries: [{ ...nq, kind: "advice" }] })).toThrow();
    expect(() => explainerLibrarySchema.parse({ entries: [{ ...nq, why_nq: undefined }] })).toThrow();
  });

  it("loads entries by key", async () => {
    serve();
    const library = await fetchExplainers();
    expect(library.get("level:overnight_high")?.title).toBe("Overnight high");
    expect(library.get("instrument:ES")).toBeUndefined();
  });
});

describe("hover-card content", () => {
  it("shows what it is, why it matters for NQ, and both directions with the core's labels", () => {
    expect(explainerLines(nq)).toEqual([
      "Futures on the Nasdaq 100.",
      "Why it matters for NQ: This is the market the desk follows.",
      "Rising: Buyers are paying up for big tech.",
      "Falling: Sellers are in charge for now.",
    ]);
    expect(explainerLines(cash)).toEqual(["The US stock market is open.", "Why it matters for NQ: Most of the volume."]);
  });

  it("replaces the short explainer when the entry is loaded, and falls back without it", () => {
    const asOf = (iso: string) => iso;
    expect(quoteLines(quote, asOf)[0]).toBe("Short form from the quote payload.");
    const rich = quoteLines(quote, asOf, nq);
    expect(rich.slice(0, 4)).toEqual(explainerLines(nq));
    expect(rich).not.toContain("Short form from the quote payload.");

    const level = levels.levels.find((l) => l.key === "overnight_high")!;
    expect(levelLines(level)).toContain(level.explainer);
    const richLevel = levelLines(level, overnightHigh);
    expect(richLevel).toContain("Above it: A break above is usually read as strength.");
    expect(richLevel).not.toContain(level.explainer);
    expect(richLevel.at(-1)).toBe("A rule of thumb, not advice.");
  });

  it("a quote tile's hover card shows the library entry once it loads", async () => {
    const fetcher = serve();
    render(<QuoteStrip quotes={[quote]} sparklines={null} asOf={(iso) => iso} />);
    fireEvent.mouseEnter(screen.getByRole("button", { name: /Nasdaq 100 futures/ }).parentElement!);
    await waitFor(() => expect(screen.getByRole("tooltip")).toHaveTextContent("Why it matters for NQ: This is the market the desk follows."));
    expect(screen.getByRole("tooltip")).toHaveTextContent("Falling: Sellers are in charge for now.");
    expect(fetcher).toHaveBeenCalledWith("/api/v1/explainers", expect.anything());
  });

  it("a key level's hover card shows the library entry", async () => {
    serve();
    render(<KeyLevelsCard levels={levels} error={null} />);
    fireEvent.click(screen.getByRole("button", { name: /Overnight high/ }));
    await waitFor(() => expect(screen.getByRole("tooltip")).toHaveTextContent("Above it: A break above is usually read as strength."));
  });

  it("keeps the payload's short explainer when the library cannot load", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("", { status: 503 })));
    render(<QuoteStrip quotes={[quote]} sparklines={null} asOf={(iso) => iso} />);
    fireEvent.mouseEnter(screen.getByRole("button", { name: /Nasdaq 100 futures/ }).parentElement!);
    expect(screen.getByRole("tooltip")).toHaveTextContent("Short form from the quote payload.");
  });
});
