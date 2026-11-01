import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { sparkPath } from "../../../web/src/chart/Sparkline";
import { ChartState, countdown, KeyLevelsCard, NextEventCard, QuoteStrip, RiskBanner, SessionTimeline } from "../../../web/src/today/Market";
import type { Quote } from "../../../web/src/today/api";
import { bars, calendar, levels, pins, risk, sparklines } from "../chartFixtures";

const quote = (over: Partial<Quote>): Quote => ({
  symbol: "NQ", label: "Nasdaq 100 futures", value: 29_173.75, value_text: "29,173.75", change_text: "+0.11%",
  direction: "up", as_of: "2026-10-22T13:40:00Z", delayed: true, stale: false, closed: false, closed_text: null,
  explainer: "Futures on the Nasdaq 100.", ...over,
});
const asOf = (iso: string) => `As of ${iso}`;

describe("sparklines", () => {
  it("break the line at a missing bucket instead of bridging it", () => {
    expect(sparkPath([1, 2, null, 3, 2], 100, 30)).toBe("M0.00 29.00 L25.00 15.00 M75.00 1.00 L100.00 15.00");
  });
  it("draw nothing with fewer than two values", () => {
    expect(sparkPath([], 100, 30)).toBe("");
    expect(sparkPath([null, 5], 100, 30)).toBe("");
  });
});

describe("chart state", () => {
  const state = (over: Partial<typeof bars>) => ({ data: { ...bars, ...over }, error: null, loading: false });
  it("says markets closed instead of the stale warning while the market is shut", () => {
    render(<ChartState bars={state({ status: "stale", market_closed: true })} />);
    expect(screen.getByRole("status")).toHaveTextContent("Markets closed · showing the last session.");
    expect(screen.queryByText(/Stale/)).toBeNull();
  });
  it("still warns about a stale quote while the market is open", () => {
    render(<ChartState bars={state({ status: "stale", market_closed: false })} />);
    expect(screen.getByRole("status")).toHaveTextContent(/^Stale:/);
  });
});

describe("quote tiles", () => {
  it("show the core's sparkline and explain the instrument on click", () => {
    render(<QuoteStrip quotes={[quote({})]} sparklines={sparklines} asOf={asOf} />);
    expect(screen.getByRole("img", { name: "Nasdaq 100 futures, last 24 hours" })).toBeInTheDocument();
    const tile = screen.getByRole("button", { name: /Nasdaq 100 futures/ });
    fireEvent.mouseEnter(tile.parentElement!);
    expect(screen.queryByRole("tooltip")).toBeNull();
    fireEvent.click(tile);
    const card = screen.getByRole("tooltip");
    expect(card).toHaveTextContent("Futures on the Nasdaq 100.");
    expect(card).toHaveTextContent("Delayed quote");
    expect(screen.getByText(/about 10 minutes delayed/)).toBeInTheDocument();
  });

  it("read 'Markets closed' while the market is shut, with no stale styling", () => {
    render(<QuoteStrip quotes={[quote({ closed: true, closed_text: "Markets closed", delayed: false })]} sparklines={null} asOf={asOf} />);
    expect(screen.getByText("Markets closed")).toBeInTheDocument();
    expect(screen.queryByText("stale")).toBeNull();
    const tile = screen.getByRole("button", { name: /Nasdaq 100 futures/ });
    expect(tile.closest(".is-stale")).toBeNull();
    fireEvent.click(tile);
    expect(screen.getByRole("tooltip")).toHaveTextContent("last value from the latest session");
  });

  it("read 'cash closed' for a closed cash index, not 'stale'", () => {
    render(<QuoteStrip quotes={[quote({ symbol: "VIX", label: "VIX fear gauge", stale: true, closed: true, delayed: false })]} sparklines={null} asOf={asOf} />);
    expect(screen.getByText("cash closed")).toBeInTheDocument();
    expect(screen.queryByText("stale")).toBeNull();
    const tile = screen.getByRole("button", { name: /VIX fear gauge/ });
    expect(tile.className).toContain("is-closed");
    expect(tile.className).not.toContain("is-stale");
  });

  it("mark a stale quote as stale and an unavailable change as a dash", () => {
    render(<QuoteStrip quotes={[quote({ stale: true }), quote({ symbol: "ES", label: "S&P 500 futures", change_text: null, direction: "unknown", delayed: false })]} sparklines={null} asOf={asOf} />);
    expect(screen.getByText("stale")).toBeInTheDocument();
    expect(screen.getByText("—")).toBeInTheDocument();
  });
});

describe("risk banner", () => {
  it("leads with the strongest reason and explains the rules", () => {
    render(<RiskBanner tone={risk} error={null} />);
    const banner = screen.getByRole("button", { name: /Risk tone: mixed/ });
    expect(banner).toHaveTextContent("Risk tone: mixed — 2-year yield +2.5bp: rising yields usually weigh on Nasdaq stocks; NQ +0.30% since the settle: rising, a sign of appetite for risk.");
    expect(banner.className).toContain("tone-warn");
    fireEvent.click(banner);
    expect(screen.getByRole("tooltip")).toHaveTextContent("Rules: NQ ±0.25%");
  });

  it("says when the tone is unclear or unavailable", () => {
    const closed = render(<RiskBanner tone={{ ...risk, tone: "closed", label: "markets closed",
      reasons: ["Markets closed · risk tone resumes when futures reopen.", ...risk.reasons] }} error={null} />);
    expect(screen.getByText(/Risk tone: markets closed/).parentElement).toHaveTextContent(
      "Risk tone: markets closed — Markets closed · risk tone resumes when futures reopen.");
    expect(screen.queryByText(/unclear/)).toBeNull();
    closed.unmount();
    const { rerender } = render(<RiskBanner tone={{ ...risk, tone: "unclear", label: "unclear" }} error={null} />);
    expect(screen.getByRole("button", { name: /Risk tone: unclear/ })).toHaveTextContent("not enough fresh quotes to judge");
    rerender(<RiskBanner tone={null} error="HTTP 503" />);
    expect(screen.getByRole("status")).toHaveTextContent("Risk tone unavailable: HTTP 503");
  });
});

describe("key levels card", () => {
  it("lists the levels, marks 'so far' and shows unavailable as unavailable", () => {
    render(<KeyLevelsCard levels={levels} error={null} />);
    const card = screen.getByRole("region", { name: "Key levels" });
    expect(within(card).getByText("Overnight high (so far)")).toBeInTheDocument();
    expect(within(card).getByText("unavailable")).toHaveClass("t-muted");
    expect(within(card).getByText("+25.00 (+0.09%)")).toBeInTheDocument();
    expect(within(card).queryByText("Prior week high")).toBeNull();
    fireEvent.click(within(card).getByRole("button", { name: /Prior day high/ }));
    expect(screen.getByRole("tooltip")).toHaveTextContent("Unavailable: no stored quotes for this window.");
  });
});

describe("next high-impact event", () => {
  const event = { at: "2026-10-29T12:30:00Z", title: "PCE + GDP", importance: "critical", kind: "economic" as const,
    explainer: "The Fed's preferred inflation measure.", forecast_text: "PCE: forecast 0.3%", playbook_text: "PCE, first 15 min: Above forecast (3): NQ fell 2, rose 1." };

  it("counts down to the core's release time", () => {
    const now = Date.parse("2026-10-23T22:24:00Z");
    expect(countdown(event.at, now)).toBe("in 5d 14h 06m");
    expect(countdown(event.at, Date.parse("2026-10-29T12:00:00Z"))).toBe("in 30m");
    expect(countdown(event.at, Date.parse("2026-10-29T13:00:00Z"))).toBe("now");
    render(<NextEventCard event={event} now={now} />);
    expect(screen.getByText("in 5d 14h 06m")).toBeInTheDocument();
    expect(screen.getByText(/Thu 08:30 ET/)).toBeInTheDocument();
    expect(screen.getByText(/NQ fell 2, rose 1/)).toBeInTheDocument();
  });

  it("says when nothing is scheduled", () => {
    render(<NextEventCard event={null} now={Date.now()} />);
    expect(screen.getByText(/Nothing high-impact scheduled/)).toBeInTheDocument();
  });
});

describe("session timeline", () => {
  it("explains a segment with the core's text", () => {
    render(<SessionTimeline calendar={calendar} pins={[]} now={Date.parse("2026-10-22T13:45:00Z")} />);
    fireEvent.click(screen.getByRole("button", { name: /US cash/ }));
    expect(screen.getByRole("tooltip")).toHaveTextContent("The US stock market is open.");
  });

  it("lays out the core's segments with a now marker and release pins", () => {
    render(<SessionTimeline calendar={calendar} pins={pins.pins} now={Date.parse("2026-10-22T13:45:00Z")} />);
    expect(screen.getByText("Globex open 18:00")).toBeInTheDocument();
    expect(screen.getByText("US cash 09:30–16:00")).toBeInTheDocument();
    expect(screen.getByLabelText("Now")).toBeInTheDocument();
    expect(screen.getByText(/Daily break 17:00–18:00 ET/)).toBeInTheDocument();
    expect(screen.getByText(/Closed Thu, Nov 26: Thanksgiving Day/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Jobless claims" }));
    const card = screen.getByRole("dialog");
    expect(card).toHaveTextContent("Thu 08:30 ET · Jobless claims");
    expect(within(card).getByRole("link", { name: "Read the note →" })).toBeInTheDocument();
  });
});
