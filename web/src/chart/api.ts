import { z } from "zod";

// Chart, session, levels, risk tone, pins and sparklines: all computed by the
// core (core/api/chart.py). The web app validates them here and only draws.

const time = z.string().min(1);

export const INTERVALS = ["5m", "15m", "1h"] as const;
export const RANGES = ["1d", "5d", "1m"] as const;
export type Interval = typeof INTERVALS[number];
export type ChartRange = typeof RANGES[number];

const segmentSchema = z.object({
  name: z.enum(["asia", "europe", "premarket", "cash", "after_hours"]),
  label: z.string(),
  starts_at: time,
  ends_at: time,
  explainer: z.string().nullable(),
});

const sessionSchema = z.object({
  trade_date: z.string(),
  opens_at: time,
  closes_at: time,
  // Null on a calendar without cash hours (24x7 crypto).
  rth_opens_at: time.nullable(),
  rth_closes_at: time.nullable(),
  early_close: z.boolean(),
  segments: z.array(segmentSchema),
});

const price = z.number().finite().nullable();

const barSchema = z.object({
  time,
  open: price,
  high: price,
  low: price,
  close: price,
  count: z.number().int().nonnegative(),
  change_pct: price,
  delayed: z.boolean(),
  source: z.string().nullable(),
  contract: z.string().nullable(),
});

export const barsSchema = z.object({
  symbol: z.string(),
  label: z.string(),
  unit: z.string(),
  interval: z.enum(INTERVALS),
  range: z.enum(RANGES),
  generated_at: time,
  start: time,
  end: time,
  sessions: z.array(sessionSchema),
  bars: z.array(barSchema),
  last_at: time.nullable(),
  delayed: z.boolean(),
  feed_status: z.string(),
  status: z.string(),
  market_closed: z.boolean().optional(),
  sources: z.array(z.string()),
  contracts: z.array(z.string()),
  fine_since: time,
  note: z.string().nullable(),
});

export const sessionCalendarSchema = z.object({
  now: time,
  phase: z.enum(["rth", "overnight", "maintenance", "closed"]),
  text: z.string(),
  session: sessionSchema,
  maintenance_starts_at: time.nullable(),
  maintenance_ends_at: time.nullable(),
  next_open_at: time.nullable(),
  holidays: z.array(z.object({ date: z.string(), name: z.string(), early_close: z.boolean() })),
});

const levelSchema = z.object({
  key: z.enum(["overnight_high", "overnight_low", "prior_settle", "prior_day_high", "prior_day_low",
    "prior_week_high", "prior_week_low", "gap_vs_settle"]),
  label: z.string(),
  value: price,
  value_text: z.string(),
  explainer: z.string(),
  window_start: time.nullable(),
  window_end: time.nullable(),
  partial: z.boolean(),
  unavailable_reason: z.string().nullable(),
});

export const levelsSchema = z.object({
  symbol: z.string(),
  generated_at: time,
  trade_date: z.string(),
  contract: z.string().nullable(),
  delayed: z.boolean(),
  levels: z.array(levelSchema),
});

export const riskToneSchema = z.object({
  generated_at: time,
  tone: z.enum(["risk_on", "leaning_risk_on", "quiet", "mixed", "leaning_risk_off", "risk_off", "unclear", "closed"]),
  label: z.string(),
  reasons: z.array(z.string()),
  inputs: z.array(z.object({
    symbol: z.string(),
    label: z.string(),
    value: price,
    change: price,
    change_text: z.string(),
    signal: z.union([z.literal(-1), z.literal(0), z.literal(1)]).nullable(),
    used: z.boolean(),
    note: z.string(),
  })),
  rules: z.string(),
});

const pinSchema = z.object({
  at: time,
  title: z.string(),
  importance: z.enum(["high", "medium"]),
  released: z.boolean(),
  result_text: z.string().nullable(),
  explainer: z.string().nullable(),
  nq_15m: price,
  nq_15m_text: z.string().nullable(),
  note_id: z.string().min(1).nullable(),
});

export const pinsSchema = z.object({
  range: z.enum(RANGES),
  start: time,
  end: time,
  pins: z.array(pinSchema),
});

export const sparklinesSchema = z.object({
  generated_at: time,
  interval: z.enum(INTERVALS),
  lines: z.array(z.object({
    symbol: z.string(),
    points: z.array(z.object({ time, value: price })),
  })),
});

// The Chart focus and the watchlist (core/api/workspace.py). The core decides
// each instrument's label, precision and whether levels and release pins exist.
export const chartInstrumentSchema = z.object({
  symbol: z.string().min(1),
  label: z.string(),
  unit: z.string(),
  price_precision: z.number().int().min(0).max(8),
  levels: z.boolean(),
  release_pins: z.boolean(),
});

export const chartInstrumentsSchema = z.object({ instruments: z.array(chartInstrumentSchema) });

export const chartFocusSchema = z.object({
  symbol: z.string().min(1),
  updated_at: time.nullable(),
  instrument: chartInstrumentSchema,
});

export const watchlistSchema = z.object({
  name: z.string(),
  symbols: z.array(z.string().min(1)),
  updated_at: time.nullable(),
});

export type Bars = z.infer<typeof barsSchema>;
export type Bar = z.infer<typeof barSchema>;
export type ChartSession = z.infer<typeof sessionSchema>;
export type SessionCalendar = z.infer<typeof sessionCalendarSchema>;
export type KeyLevel = z.infer<typeof levelSchema>;
export type KeyLevels = z.infer<typeof levelsSchema>;
export type RiskTone = z.infer<typeof riskToneSchema>;
export type Pin = z.infer<typeof pinSchema>;
export type Pins = z.infer<typeof pinsSchema>;
export type Sparklines = z.infer<typeof sparklinesSchema>;
export type ChartInstrument = z.infer<typeof chartInstrumentSchema>;
export type ChartFocus = z.infer<typeof chartFocusSchema>;
export type Watchlist = z.infer<typeof watchlistSchema>;

async function get<T>(path: string, schema: z.ZodType<T>, signal?: AbortSignal, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { signal, cache: "no-store", ...init });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // keep the status text
    }
    throw new Error(detail);
  }
  return schema.parse(await response.json());
}

export const fetchBars = (symbol: string, interval: Interval, range: ChartRange, signal?: AbortSignal) =>
  get(`/api/v1/bars/${encodeURIComponent(symbol)}?interval=${interval}&range=${range}`, barsSchema, signal);
export const fetchSession = (signal?: AbortSignal) => get("/api/v1/session", sessionCalendarSchema, signal);
export const fetchLevels = (symbol: string, signal?: AbortSignal) =>
  get(`/api/v1/levels/${encodeURIComponent(symbol)}`, levelsSchema, signal);
export const fetchRiskTone = (signal?: AbortSignal) => get("/api/v1/risk-tone", riskToneSchema, signal);
export const fetchPins = (range: ChartRange, signal?: AbortSignal) => get(`/api/v1/chart/pins?range=${range}`, pinsSchema, signal);
/** 24-hour sparklines for `symbols`, or for the desk strip when none are named. */
export const fetchSparklines = (signal?: AbortSignal, symbols?: readonly string[]) =>
  get(`/api/v1/sparklines${symbols?.length ? `?symbols=${symbols.map(encodeURIComponent).join(",")}` : ""}`, sparklinesSchema, signal);
export const fetchChartInstruments = (signal?: AbortSignal) => get("/api/v1/chart/instruments", chartInstrumentsSchema, signal);
export const fetchFocus = (signal?: AbortSignal) => get("/api/v1/chart/focus", chartFocusSchema, signal);
export const fetchWatchlist = (signal?: AbortSignal) => get("/api/v1/watchlist", watchlistSchema, signal);
/** Save the Chart page's instrument (the core writes no note and calls no AI). */
export const saveFocus = (symbol: string) => get("/api/v1/chart/focus", chartFocusSchema, undefined, {
  method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ symbol }),
});

/** Intervals the core offers for a range (a month of 5-minute bars is not offered). */
export function intervalsFor(range: ChartRange): readonly Interval[] {
  return range === "1m" ? ["15m", "1h"] : INTERVALS;
}
