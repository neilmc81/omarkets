import { z } from "zod";

// The active Omarchy palette, served by the core from
// ~/.local/state/omarchy/current/theme/colors.toml. Each role becomes a CSS
// custom property (--os-<role>); every design token in style.css and
// today.css derives from those. Without Omarchy the stylesheet defaults apply.

export const ROLES = [
  "background", "foreground", "bright_foreground", "accent", "selection", "muted",
  "red", "green", "yellow", "orange", "blue", "cyan", "magenta",
] as const;

const hex = z.string().regex(/^#[0-9a-fA-F]{6}$/);

export const paletteSchema = z.object({
  source: z.enum(["omarchy", "fallback"]),
  name: z.string().nullable(),
  mode: z.enum(["dark", "light"]),
  version: z.string(),
  colors: z.object(Object.fromEntries(ROLES.map((role) => [role, hex])) as Record<(typeof ROLES)[number], typeof hex>),
  detail: z.string().nullable().optional(),
});

export type Palette = z.infer<typeof paletteSchema>;

export const THEME_EVENT = "omarkets-theme";

const cssName = (role: string) => `--os-${role.replace(/_/g, "-")}`;

let applied: string | null = null;

/** Write the palette onto the document; returns false when nothing changed. */
export function applyPalette(palette: Palette, root: HTMLElement = document.documentElement): boolean {
  if (applied === palette.version && root.dataset.themeVersion === palette.version) return false;
  if (palette.source === "fallback") {
    // Let the stylesheet's own defaults (dark, or light via prefers-color-scheme) apply.
    for (const role of ROLES) root.style.removeProperty(cssName(role));
    root.style.removeProperty("color-scheme");
  } else {
    for (const role of ROLES) root.style.setProperty(cssName(role), palette.colors[role]);
    root.style.setProperty("color-scheme", palette.mode);
  }
  root.dataset.themeSource = palette.source;
  root.dataset.themeMode = palette.mode;
  root.dataset.themeVersion = palette.version;
  if (palette.name) root.dataset.themeName = palette.name;
  else delete root.dataset.themeName;
  applied = palette.version;
  window.dispatchEvent(new CustomEvent(THEME_EVENT, { detail: palette }));
  return true;
}

function parse(raw: unknown): Palette | null {
  const result = paletteSchema.safeParse(raw);
  return result.success ? result.data : null;
}

/**
 * Load the palette now and keep following the desktop theme.
 *
 * A server-sent event stream delivers changes as soon as the core sees them
 * (the Omarchy theme-set hook triggers it); EventSource reconnects on its own.
 * Where EventSource is missing the palette is polled instead.
 */
export function startThemeSync(options: { pollMs?: number } = {}): () => void {
  let stopped = false;
  const load = async () => {
    try {
      const response = await fetch("/api/v1/theme", { cache: "no-store" });
      if (!response.ok) return;
      const palette = parse(await response.json());
      if (palette && !stopped) applyPalette(palette);
    } catch {
      // Core unavailable: keep the current (or default) colours.
    }
  };
  void load();

  if (typeof EventSource === "undefined") {
    const timer = window.setInterval(load, options.pollMs ?? 5000);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }
  const stream = new EventSource("/api/v1/theme/stream");
  stream.addEventListener("theme", (event) => {
    try {
      const palette = parse(JSON.parse((event as MessageEvent<string>).data));
      if (palette && !stopped) applyPalette(palette);
    } catch {
      // Ignore a malformed event; the next one replaces it.
    }
  });
  return () => {
    stopped = true;
    stream.close();
  };
}

/** Resolve a CSS custom property to a concrete colour (for canvas charts). */
export function cssColor(name: string, fallback: string): string {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

/** Test hook: forget what was applied. */
export function resetAppliedTheme(): void {
  applied = null;
}

/** Mix two #rrggbb colours: ``weight`` of ``a`` (0..1) blended into ``b``. */
export function mixHex(a: string, b: string, weight: number): string {
  const channel = (hex: string, i: number) => parseInt(hex.slice(1 + i * 2, 3 + i * 2), 16);
  const out = [0, 1, 2].map((i) => Math.round(channel(a, i) * weight + channel(b, i) * (1 - weight)));
  return `#${out.map((value) => value.toString(16).padStart(2, "0")).join("")}`;
}

export type ChartColors = { background: string; text: string; grid: string; border: string; line: string };

/** Concrete chart colours from the current palette (canvas cannot use color-mix). */
export function chartColors(): ChartColors {
  const isHex = (value: string) => /^#[0-9a-fA-F]{6}$/.test(value);
  const read = (role: string, fallback: string) => {
    const value = cssColor(cssName(role), fallback);
    return isHex(value) ? value : fallback;
  };
  const bg = read("background", "#090f18");
  const fg = read("foreground", "#dbe5f1");
  return {
    background: mixHex(fg, bg, 0.04),
    text: mixHex(fg, bg, 0.7),
    grid: mixHex(fg, bg, 0.08),
    border: mixHex(fg, bg, 0.17),
    line: read("accent", "#71d6ff"),
  };
}

export type ChartPalette = ChartColors & {
  up: string; down: string; warn: string; yellow: string; blue: string; cyan: string; magenta: string;
  muted: string; shade: string; crosshair: string;
  /** Three overlay colours, as distinct as the theme allows (see overlayColors). */
  overlays: [string, string, string];
};

const distance = (a: string, b: string) => {
  const channel = (hex: string, i: number) => parseInt(hex.slice(1 + i * 2, 3 + i * 2), 16);
  return Math.hypot(...[0, 1, 2].map((i) => channel(a, i) - channel(b, i)));
};

/**
 * Pick overlay colours from the theme's roles, each as far as possible from
 * the candle colours and the ones already picked. Some themes give blue,
 * yellow and green nearly the same colour, so fixed roles could collide.
 */
export function overlayColors(candidates: string[], avoid: string[]): [string, string, string] {
  const picked: string[] = [];
  for (let i = 0; i < 3; i++) {
    const taken = [...avoid, ...picked];
    const best = candidates.filter((c) => !picked.includes(c))
      .reduce((a, b) => Math.min(...taken.map((t) => distance(b, t))) > Math.min(...taken.map((t) => distance(a, t))) ? b : a);
    picked.push(best);
  }
  return picked as [string, string, string];
}

/** The session chart's colours: the Omarchy roles resolved to #rrggbb for the canvas. */
export function chartPalette(): ChartPalette {
  const isHex = (value: string) => /^#[0-9a-fA-F]{6}$/.test(value);
  const read = (role: string, fallback: string) => {
    const value = cssColor(cssName(role), fallback);
    return isHex(value) ? value : fallback;
  };
  const base = chartColors();
  const bg = read("background", "#090f18");
  const fg = read("foreground", "#dbe5f1");
  const blue = read("blue", "#7aa2f7");
  return {
    ...base,
    up: read("green", "#4fd18b"),
    down: read("red", "#ff6b6b"),
    warn: read("orange", "#e0a050"),
    yellow: read("yellow", "#f2c14e"),
    blue,
    cyan: read("cyan", "#62c9ed"),
    magenta: read("magenta", "#cdb8f0"),
    muted: mixHex(fg, bg, 0.55),
    // Overnight shading: a faint tint of blue over the chart background.
    shade: mixHex(blue, base.background, 0.09),
    crosshair: mixHex(fg, bg, 0.45),
    overlays: overlayColors([blue, read("orange", "#e0a050"), read("magenta", "#cdb8f0"), read("cyan", "#62c9ed"), read("yellow", "#f2c14e")],
      [read("green", "#4fd18b"), read("red", "#ff6b6b"), base.background]),
  };
}

