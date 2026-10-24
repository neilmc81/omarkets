export const draft = {
  headline: "Inflation ran hotter than expected",
  summary: "Hotter prices make Fed cuts less likely.",
  what_it_means: "Prices rose faster than forecast.",
  market_reaction: "NQ fell 0.4% in 15 minutes.",
  rates_read: "The 2-year yield jumped 6bp.",
  watch_next: ["Fed speakers this afternoon"],
  learn: "CPI measures consumer prices.",
  nq_tone: "negative",
};

export const note = {
  id: "release:20261014T1230:CPI", kind: "release", title: "Consumer prices (CPI)",
  created_at: "2026-10-14T12:51:00Z", event_at: "2026-10-14T12:30:00Z",
  figures: [{ name: "CPI m/m", unit: "percent", actual: 0.4, forecast: 0.3, previous: 0.2, decimals: 1,
    verdict: "above", actual_text: "0.4%", forecast_text: "0.3%", previous_text: "0.2%",
    surprise: { value: 0.1, value_text: "+0.1 pt", size: "bigger", history_count: 10, larger_than: 9,
      text: "Bigger than usual surprise above the forecast — larger than 9 of the last 10" } }],
  moves: [{ symbol: "NQ", label: "Nasdaq 100 futures", change: -0.4, change_unit: "%",
    start_at: "2026-10-14T12:30:00Z", end_at: "2026-10-14T12:45:00Z", delayed: true, text: "-0.40%" }],
  headlines: [], upcoming: [], rates: null, draft, status: "ok", error: null,
  model: "anthropic/claude-sonnet-5.5 · desk:v2", cost_usd: 0.014,
};

export const desk = {
  generated_at: "2026-10-14T13:00:00Z",
  session_text: "US market open · closes in 6h 00m",
  quotes: [{ symbol: "NQ", label: "Nasdaq 100 futures", value: 29700, value_text: "29,700.00",
    change_text: "-0.40%", direction: "down", as_of: "2026-10-14T12:50:00Z", delayed: true, stale: false }],
  rates: { as_of: null, lines: [{ symbol: "US2Y", label: "2-year yield", value: 4.8, value_text: "4.800%",
    day_change_bp: 6, week_change_bp: null, day_text: "+6.0bp", week_text: "—" }],
  curve_bp: null, curve_text: "unavailable", explainer: "Yields explained." },
  notes: [note],
  upcoming: [{ kind: "earnings", title: "Meta earnings", at: "2026-10-21T20:05:00Z",
    when_text: "after the close", importance: "high", detail: "EPS forecast $6.67", explainer: "Meta is huge." }],
  writer_ready: true, writer_detail: null,
};

export const ok = (body: unknown) => Promise.resolve({ ok: true, status: 200, json: async () => body });
