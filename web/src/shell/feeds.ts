import type { HealthResponse } from "../api/schemas";
import type { Desk, Quote } from "../today/api";

export type FeedsTone = "ok" | "warn" | "down" | "pending";
export type FeedsStatus = { tone: FeedsTone; label: string; lines: string[] };

// Summarises the core's own health and desk flags for the header dot. It only
// reads statuses the core reports; it judges nothing about the market.
// `quotes` is the focus strip Today shows; without it, the desk's tiles.
// `focusError` is why the focus strip couldn't be read (Today then falls back to NQ).
export function feedsStatus(input: { desk: Desk | null; deskError: string | null; health: HealthResponse | null; healthError: string | null; quotes?: Quote[]; focusError?: string | null }): FeedsStatus {
  const { desk, deskError, health, healthError } = input;
  if (!desk && !health && !deskError && !healthError) return { tone: "pending", label: "Connecting…", lines: ["Waiting for the Omarkets core."] };
  if (!desk && !health) return { tone: "down", label: "Omarkets unavailable", lines: [deskError ?? healthError ?? "No response from the core."] };

  const problems: string[] = [];
  const lines: string[] = [];
  let down = false;
  if (deskError) problems.push(`Desk: ${deskError} · showing last loaded data`);
  if (healthError) problems.push(`Health check: ${healthError}`);
  if (input.focusError) problems.push(`Focus: ${input.focusError} · showing NQ and the desk's tiles`);
  if (health) {
    if (!health.database_ok) {
      down = true;
      problems.push("Database error");
    }
    if (health.provider_status !== "ok") problems.push(`Quote feed ${health.provider_status}${health.provider_error ? `: ${health.provider_error}` : ""}`);
    if (!health.stream_running) problems.push("Quote stream stopped");
    const economic = health.economic_providers.filter((p) => p.status !== "healthy");
    if (economic.length) problems.push(`Economic sources not healthy: ${economic.map((p) => `${p.name} (${p.status})`).join(", ")}`);
    if (health.news && health.news.enabled && health.news.offline_sources + health.news.degraded_sources > 0) {
      problems.push(`News sources: ${health.news.offline_sources} offline, ${health.news.degraded_sources} degraded`);
    }
    lines.push(`Quote feed: ${health.provider} · ${health.provider_status}`);
    lines.push(`Economic sources: ${health.economic_providers.length - economic.length} of ${health.economic_providers.length} healthy`);
    if (health.news?.enabled) lines.push(`News sources: ${health.news.healthy_sources} of ${health.news.source_count} healthy`);
  }
  const quotes = input.quotes ?? desk?.quotes ?? [];
  const stale = quotes.filter((q) => q.stale && !q.closed).map((q) => q.label);
  if (stale.length) problems.push(`Stale quotes: ${stale.join(", ")}`);
  if (quotes.some((q) => q.delayed)) lines.push("Some quotes are delayed by the source (normal).");
  if (desk) {
    if (!desk.writer_ready) problems.push(`AI writer not available${desk.writer_detail ? `: ${desk.writer_detail}` : ""}`);
  }
  if (!problems.length) return { tone: "ok", label: "All feeds OK", lines };
  return {
    tone: down ? "down" : "warn",
    label: problems.length === 1 ? "1 issue" : `${problems.length} issues`,
    lines: [...problems, ...lines],
  };
}
