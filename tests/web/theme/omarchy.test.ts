import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  applyPalette, chartColors, mixHex, overlayColors, resetAppliedTheme, startThemeSync, THEME_EVENT, type Palette,
} from "../../../web/src/theme/omarchy";

const colors = (overrides: Partial<Palette["colors"]> = {}): Palette["colors"] => ({
  background: "#111c18", foreground: "#c1c497", bright_foreground: "#f7e8b2", accent: "#509475",
  selection: "#32473b", muted: "#53685b", red: "#ff5345", green: "#549e6a", yellow: "#459451",
  orange: "#a2734b", blue: "#509475", cyan: "#2dd5b7", magenta: "#d2689c", ...overrides,
});

const osaka: Palette = { source: "omarchy", name: "osaka-jade", mode: "dark", version: "v1", colors: colors() };
const white: Palette = {
  source: "omarchy", name: "white", mode: "light", version: "v2",
  colors: colors({ background: "#ffffff", foreground: "#000000", bright_foreground: "#000000", accent: "#6e6e6e" }),
};
const fallback: Palette = { source: "fallback", name: null, mode: "dark", version: "f", colors: colors(), detail: "no omarchy" };

class FakeEventSource {
  static last: FakeEventSource | null = null;
  listeners: Record<string, ((event: MessageEvent<string>) => void)[]> = {};
  closed = false;
  constructor(public url: string) { FakeEventSource.last = this; }
  addEventListener(type: string, listener: (event: MessageEvent<string>) => void) {
    (this.listeners[type] ??= []).push(listener);
  }
  emit(type: string, data: unknown) {
    for (const listener of this.listeners[type] ?? []) listener(new MessageEvent(type, { data: JSON.stringify(data) }));
  }
  close() { this.closed = true; }
}

const root = () => document.documentElement;

beforeEach(() => {
  resetAppliedTheme();
  root().removeAttribute("style");
  for (const key of Object.keys(root().dataset)) delete root().dataset[key];
});
afterEach(() => { FakeEventSource.last = null; });

describe("applyPalette", () => {
  it("writes every role as a CSS variable and marks the source", () => {
    expect(applyPalette(osaka)).toBe(true);
    expect(root().style.getPropertyValue("--os-background")).toBe("#111c18");
    expect(root().style.getPropertyValue("--os-bright-foreground")).toBe("#f7e8b2");
    expect(root().style.getPropertyValue("--os-green")).toBe("#549e6a");
    expect(root().style.getPropertyValue("color-scheme")).toBe("dark");
    expect(root().dataset.themeSource).toBe("omarchy");
    expect(root().dataset.themeName).toBe("osaka-jade");
    expect(applyPalette(osaka)).toBe(false);
  });

  it("switches to a light theme and announces the change", () => {
    applyPalette(osaka);
    const seen: Palette[] = [];
    const listener = (event: Event) => seen.push((event as CustomEvent<Palette>).detail);
    window.addEventListener(THEME_EVENT, listener);
    applyPalette(white);
    window.removeEventListener(THEME_EVENT, listener);
    expect(root().style.getPropertyValue("--os-background")).toBe("#ffffff");
    expect(root().style.getPropertyValue("color-scheme")).toBe("light");
    expect(root().dataset.themeMode).toBe("light");
    expect(seen.map((p) => p.name)).toEqual(["white"]);
  });

  it("removes overrides for the fallback so the stylesheet defaults apply", () => {
    applyPalette(osaka);
    applyPalette(fallback);
    expect(root().style.getPropertyValue("--os-background")).toBe("");
    expect(root().dataset.themeSource).toBe("fallback");
    expect(root().dataset.themeName).toBeUndefined();
  });
});

describe("startThemeSync", () => {
  it("loads the theme at start and follows changes from the stream", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => osaka });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("EventSource", FakeEventSource);
    const stop = startThemeSync();
    await vi.waitFor(() => expect(root().dataset.themeName).toBe("osaka-jade"));
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/theme", { cache: "no-store" });
    expect(FakeEventSource.last?.url).toBe("/api/v1/theme/stream");

    FakeEventSource.last!.emit("theme", white);
    expect(root().dataset.themeName).toBe("white");
    expect(root().style.getPropertyValue("--os-background")).toBe("#ffffff");

    // A malformed event is ignored rather than breaking the page.
    FakeEventSource.last!.emit("theme", { colors: { background: "red" } });
    expect(root().dataset.themeName).toBe("white");
    stop();
    expect(FakeEventSource.last!.closed).toBe(true);
  });

  it("keeps the default colours when the core is unreachable", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("offline")));
    vi.stubGlobal("EventSource", FakeEventSource);
    const stop = startThemeSync();
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(root().dataset.themeSource).toBeUndefined();
    expect(root().style.getPropertyValue("--os-background")).toBe("");
    stop();
  });

  it("polls when EventSource is unavailable", async () => {
    vi.useFakeTimers();
    let current: Palette = osaka;
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => current })));
    vi.stubGlobal("EventSource", undefined);
    const stop = startThemeSync({ pollMs: 1000 });
    await vi.waitFor(() => expect(root().dataset.themeName).toBe("osaka-jade"));
    current = white;
    await vi.advanceTimersByTimeAsync(1000);
    expect(root().dataset.themeName).toBe("white");
    stop();
  });
});

describe("chart colours", () => {
  it("mixes hex colours and follows the applied palette", () => {
    expect(mixHex("#ffffff", "#000000", 0.5)).toBe("#808080");
    applyPalette(white);
    const chart = chartColors();
    expect(chart.line).toBe("#6e6e6e");
    expect(chart.background).toBe(mixHex("#000000", "#ffffff", 0.04));
  });
});

describe("overlay colours", () => {
  it("stay distinct when a theme gives several roles the same hue (osaka-jade)", () => {
    // blue, orange, magenta, cyan, yellow; candles use green and red.
    const picked = overlayColors(["#509475", "#a2734b", "#d2689c", "#2dd5b7", "#459451"], ["#549e6a", "#ff5345", "#111c18"]);
    expect(new Set(picked).size).toBe(3);
    expect(picked).not.toContain("#509475"); // blue is almost the up colour here
    expect(picked).not.toContain("#459451"); // and so is yellow
  });
});
