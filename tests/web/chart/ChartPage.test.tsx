import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ChartPage } from "../../../web/src/chart/ChartPage";
import type { Overlay } from "../../../web/src/chart/SessionChart";
import { ok } from "../deskFixtures";
import { bars, esBars, levels, pins } from "../chartFixtures";

const drawn = vi.hoisted(() => ({ props: null as null | { overlays?: Overlay[]; levels?: unknown[]; pins?: unknown[]; label: string } }));
vi.mock("../../../web/src/chart/SessionChart", () => ({
  SessionChart: (props: { overlays?: Overlay[]; levels?: unknown[]; pins?: unknown[]; label: string }) => {
    drawn.props = props;
    return <div role="img" aria-label={props.label} />;
  },
}));

function serve(over: Record<string, () => Promise<unknown>> = {}) {
  const fetcher = vi.fn((url: string) => {
    for (const [prefix, answer] of Object.entries(over)) if (url.startsWith(prefix)) return answer();
    if (url.startsWith("/api/v1/bars/ES")) return ok(esBars);
    if (url.startsWith("/api/v1/bars/")) return ok({ ...bars, interval: url.includes("15m") ? "15m" : "5m", range: url.includes("range=1m") ? "1m" : url.includes("5d") ? "5d" : "1d" });
    if (url.startsWith("/api/v1/levels/")) return ok(levels);
    if (url.startsWith("/api/v1/chart/pins")) return ok(pins);
    return Promise.reject(new Error(`unexpected ${url}`));
  });
  vi.stubGlobal("fetch", fetcher);
  return fetcher;
}

describe("Chart page", () => {
  it("draws NQ with every price level and the pins, and lists both for keyboard users", async () => {
    serve();
    render(<ChartPage />);
    await waitFor(() => expect(drawn.props?.pins).toHaveLength(2));
    expect(screen.getByRole("img", { name: /NQ candles, 5 sessions, 15m bars/ })).toBeInTheDocument();
    expect(drawn.props?.levels).toHaveLength(7); // all but the gap
    const pinLink = screen.getByRole("button", { name: /Thu 08:30 ET · Jobless claims/ });
    fireEvent.click(pinLink);
    expect(within(screen.getByRole("dialog")).getByRole("link", { name: "Read the note →" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Charts by TradingView" })).toBeInTheDocument();
    expect(screen.getByText("Delayed about 10 minutes")).toBeInTheDocument();
  });

  it("fetches an overlay only when it is switched on", async () => {
    const fetcher = serve();
    render(<ChartPage />);
    await waitFor(() => expect(drawn.props).not.toBeNull());
    expect(fetcher.mock.calls.some(([url]) => String(url).startsWith("/api/v1/bars/ES"))).toBe(false);
    fireEvent.click(screen.getByRole("checkbox", { name: "S&P 500 futures" }));
    await waitFor(() => expect(drawn.props?.overlays?.map((o) => o.key)).toEqual(["ES"]));
    expect(fetcher).toHaveBeenCalledWith("/api/v1/bars/ES?interval=15m&range=5d", expect.anything());
    fireEvent.click(screen.getByRole("checkbox", { name: "S&P 500 futures" }));
    await waitFor(() => expect(drawn.props?.overlays).toEqual([]));
  });

  it("offers 15-minute and hourly bars only for one month", async () => {
    const fetcher = serve();
    render(<ChartPage />);
    fireEvent.change(screen.getByRole("combobox", { name: "Bar size" }), { target: { value: "5m" } });
    fireEvent.click(screen.getByRole("button", { name: "1M" }));
    await waitFor(() => expect(fetcher).toHaveBeenCalledWith("/api/v1/bars/NQ?interval=15m&range=1m", expect.anything()));
    const options = within(screen.getByRole("combobox", { name: "Bar size" })).getAllByRole("option").map((o) => o.textContent);
    expect(options).toEqual(["15m", "1h"]);
  });

  it("shows stale and unavailable data plainly", async () => {
    serve({
      "/api/v1/bars/NQ": () => ok({ ...bars, status: "stale" }),
      "/api/v1/levels/": () => Promise.resolve({ ok: false, status: 503, json: async () => ({ detail: "storage unavailable" }) }),
    });
    render(<ChartPage />);
    expect(await screen.findByText(/Stale: no fresh NQ quote recently/)).toBeInTheDocument();
    expect(await screen.findByText("unavailable (storage unavailable)")).toBeInTheDocument();
  });
});
