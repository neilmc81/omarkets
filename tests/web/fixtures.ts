import type { HealthResponse } from "../../web/src/api/schemas";

export const time = "2026-09-17T12:00:00.123456Z";
export const health: HealthResponse = {
  status: "ok", app: "omarkets", version: "0.1.0", time, database_ok: true, provider: "fake", provider_status: "ok", provider_error: null,
  stream_running: true, cached_instruments: 18, updates_published: 1, last_update_at: time, subscriber_count: 1, economic_providers: [],
  news: { enabled: true, source_count: 2, healthy_sources: 2, degraded_sources: 0, offline_sources: 0, disabled_sources: 0, event_count: 2, subscriber_count: 0 },
};
