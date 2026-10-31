import type { Bars, ChartInstrument, KeyLevels, Pins, RiskTone, SessionCalendar, Sparklines } from "../../web/src/chart/api";

// Thursday Oct 22 2026: session opened Wed 18:00 ET (22:00 UTC).
const session = {
  trade_date: "2026-10-22",
  opens_at: "2026-10-21T22:00:00Z",
  closes_at: "2026-10-22T21:00:00Z",
  rth_opens_at: "2026-10-22T13:30:00Z",
  rth_closes_at: "2026-10-22T20:00:00Z",
  early_close: false,
  segments: [
    { name: "asia" as const, label: "Asia", starts_at: "2026-10-21T22:00:00Z", ends_at: "2026-10-22T07:00:00Z", explainer: "Futures reopen at 18:00 ET. Asian markets trade now." },
    { name: "europe" as const, label: "Europe", starts_at: "2026-10-22T07:00:00Z", ends_at: "2026-10-22T12:00:00Z", explainer: "European markets open." },
    { name: "premarket" as const, label: "Pre-market", starts_at: "2026-10-22T12:00:00Z", ends_at: "2026-10-22T13:30:00Z", explainer: "The US pre-market. The 08:30 ET economic data comes out here and often moves NQ." },
    { name: "cash" as const, label: "US cash", starts_at: "2026-10-22T13:30:00Z", ends_at: "2026-10-22T20:00:00Z", explainer: "The US stock market is open." },
    { name: "after_hours" as const, label: "After the cash close", starts_at: "2026-10-22T20:00:00Z", ends_at: "2026-10-22T21:00:00Z", explainer: "After the US cash close, futures trade on until 17:00 ET." },
  ],
};

const bar = (time: string, close: number, change: number) => ({
  time, open: close - 2, high: close + 3, low: close - 4, close, count: 50, change_pct: change,
  delayed: true, source: "cnbc", contract: "NQZ26",
});
const gap = (time: string) => ({ time, open: null, high: null, low: null, close: null, count: 0, change_pct: null, delayed: false, source: null, contract: null });

export const bars: Bars = {
  symbol: "NQ", label: "Nasdaq 100 futures", unit: "index points", interval: "5m", range: "1d",
  generated_at: "2026-10-22T13:45:00Z", start: "2026-10-21T22:00:00Z", end: "2026-10-22T13:45:00Z",
  sessions: [session],
  bars: [bar("2026-10-22T12:25:00Z", 29_150, 0), bar("2026-10-22T12:30:00Z", 29_120, -0.1), gap("2026-10-22T12:35:00Z"), bar("2026-10-22T12:40:00Z", 29_140, -0.03)],
  last_at: "2026-10-22T12:44:10Z", delayed: true, feed_status: "delayed", status: "live", market_closed: false,
  sources: ["cnbc"], contracts: ["NQZ26"], fine_since: "2026-09-22T13:45:00Z", note: null,
};

export const esBars: Bars = { ...bars, symbol: "ES", label: "S&P 500 futures", bars: [bar("2026-10-22T12:25:00Z", 7700, 0), bar("2026-10-22T12:30:00Z", 7690, -0.13)] };

const level = (key: KeyLevels["levels"][number]["key"], label: string, value: number | null, extra = {}) => ({
  key, label, value, value_text: value === null ? "unavailable" : value.toLocaleString("en-US", { minimumFractionDigits: 2 }),
  explainer: `${label}: what it usually means.`, window_start: null, window_end: null, partial: false,
  unavailable_reason: value === null ? "no stored quotes for this window" : null, ...extra,
});

export const levels: KeyLevels = {
  symbol: "NQ", generated_at: "2026-10-22T13:45:00Z", trade_date: "2026-10-22", contract: "NQZ26", delayed: true,
  levels: [
    level("overnight_high", "Overnight high", 29_220, { partial: true }),
    level("overnight_low", "Overnight low", 29_080),
    level("prior_settle", "Prior settle", 29_149),
    level("prior_day_high", "Prior day high", null),
    level("prior_day_low", "Prior day low", 28_910),
    level("prior_week_high", "Prior week high", 30_212),
    level("prior_week_low", "Prior week low", 29_142),
    { ...level("gap_vs_settle", "Gap vs settle", 25), value_text: "+25.00 (+0.09%)" },
  ],
};

export const pins: Pins = {
  range: "1d", start: "2026-10-21T22:00:00Z", end: "2026-10-22T21:00:00Z",
  pins: [
    { at: "2026-10-22T12:30:00Z", title: "Jobless claims", importance: "high", released: true, result_text: "Lower than forecast",
      explainer: "Weekly count of new unemployment filings.", nq_15m: -0.06, nq_15m_text: "NQ -0.06% in 15 min", note_id: "release:20261022T1230:INITIAL_CLAIMS" },
    { at: "2026-10-22T14:00:00Z", title: "Existing home sales", importance: "medium", released: false, result_text: null,
      explainer: null, nq_15m: null, nq_15m_text: null, note_id: null },
  ],
};

export const risk: RiskTone = {
  generated_at: "2026-10-22T13:45:00Z", tone: "mixed", label: "mixed",
  reasons: ["2-year yield +2.5bp: rising yields usually weigh on Nasdaq stocks.", "NQ +0.30% since the settle: rising, a sign of appetite for risk.", "Dollar +0.03%: little changed."],
  inputs: [
    { symbol: "NQ", label: "NQ futures", value: 29_170, change: 0.3, change_text: "+0.30%", signal: 1, used: true, note: "" },
    { symbol: "US2Y", label: "2-year yield", value: 4.66, change: 2.5, change_text: "+2.5bp", signal: -1, used: true, note: "" },
    { symbol: "DXY", label: "US dollar", value: 101.6, change: 0.03, change_text: "+0.03%", signal: 0, used: true, note: "" },
    { symbol: "VIX", label: "VIX", value: null, change: null, change_text: "—", signal: null, used: false, note: "" },
  ],
  rules: "Rules: NQ ±0.25%, 2-year yield ±3bp, dollar ±0.25%, VIX ±5% (or at 25+) since the previous close.",
};

export const calendar: SessionCalendar = {
  now: "2026-10-22T13:45:00Z", phase: "rth", text: "US market open · closes in 6h 15m", session,
  maintenance_starts_at: "2026-10-22T21:00:00Z", maintenance_ends_at: "2026-10-22T22:00:00Z", next_open_at: null,
  holidays: [{ date: "2026-11-26", name: "Thanksgiving Day", early_close: false }],
};

export const sparklines: Sparklines = {
  generated_at: "2026-10-22T13:45:00Z", interval: "15m",
  lines: [{ symbol: "NQ", points: [{ time: "2026-10-22T12:00:00Z", value: 29_100 }, { time: "2026-10-22T12:15:00Z", value: null }, { time: "2026-10-22T12:30:00Z", value: 29_150 }, { time: "2026-10-22T12:45:00Z", value: 29_140 }] }],
};

// Chart focus and the watchlist (core/api/workspace.py).
const chartInstrument = (symbol: string, label: string, extra: Partial<ChartInstrument> = {}): ChartInstrument => ({
  symbol, label, unit: "index points", price_precision: 2, levels: false, release_pins: false, ...extra,
});
export const chartInstruments: { instruments: ChartInstrument[] } = {
  instruments: [
    chartInstrument("NQ", "Nasdaq 100 futures", { levels: true, release_pins: true }),
    chartInstrument("ES", "S&P 500 futures", { levels: true }),
    chartInstrument("US2Y", "2-year yield", { unit: "percent", price_precision: 3 }),
    chartInstrument("US10Y", "10-year yield", { unit: "percent", price_precision: 3 }),
    chartInstrument("DXY", "US dollar", { price_precision: 3 }),
    chartInstrument("BTC", "Bitcoin", { unit: "USD" }),
  ],
};
export const focusOf = (symbol: string) => ({
  symbol, updated_at: null, instrument: chartInstruments.instruments.find((i) => i.symbol === symbol)!,
});
export const watchlist = { name: "Desk strip", symbols: ["NQ", "ES", "US10Y", "US2Y", "DXY", "VIX", "WTI", "GOLD"], updated_at: null };

// Bitcoin: a UTC day with no cash hours.
export const btcBars: Bars = {
  ...bars, symbol: "BTC", label: "Bitcoin", unit: "USD", feed_status: "live", delayed: false, sources: ["binance"], contracts: [],
  sessions: [{ trade_date: "2026-10-22", opens_at: "2026-10-22T00:00:00Z", closes_at: "2026-10-23T00:00:00Z",
    rth_opens_at: null, rth_closes_at: null, early_close: false, segments: [] }],
  bars: [{ time: "2026-10-22T12:25:00Z", open: 110_000, high: 110_300, low: 109_900, close: 110_250, count: 40, change_pct: 0,
    delayed: false, source: "binance", contract: null }],
};
export const esLevels: KeyLevels = { ...levels, symbol: "ES", contract: "ESZ26" };
