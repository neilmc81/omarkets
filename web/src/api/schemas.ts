import { z } from "zod";

// The core's /health payload, read by the feeds dot and the status panel.

const timestamp = z.string().datetime({ offset: true });
const count = z.number().int().nonnegative();
const nullableTime = timestamp.nullable().default(null);

export const economicProviderSchema = z.object({
  name: z.string().min(1),
  status: z.enum(["healthy", "degraded", "unavailable", "not_configured"]),
  checked_at: nullableTime,
  detail: z.string().nullable().default(null),
});

export const healthResponseSchema = z.object({
  status: z.string(),
  app: z.string(),
  version: z.string(),
  time: timestamp,
  database_ok: z.boolean(),
  provider: z.string(),
  provider_status: z.string(),
  provider_error: z.string().nullable().default(null),
  stream_running: z.boolean(),
  cached_instruments: count,
  updates_published: count,
  last_update_at: nullableTime,
  subscriber_count: count,
  economic_providers: z.array(economicProviderSchema).default([]),
  news: z.object({
    enabled: z.boolean(),
    source_count: count,
    healthy_sources: count,
    degraded_sources: count,
    offline_sources: count,
    disabled_sources: count,
    event_count: count,
    subscriber_count: count,
  }).nullable().default(null),
});

export type HealthResponse = z.infer<typeof healthResponseSchema>;
export type EconomicProvider = z.infer<typeof economicProviderSchema>;

// /api/v1/instruments, read by the Instruments table in the status panel.
export const instrumentRowSchema = z.object({
  symbol: z.string().min(1),
  display_name: z.string(),
  asset_class: z.string(),
  venue: z.string(),
  currency: z.string().nullable(),
  session_calendar: z.string(),
  session_calendar_label: z.string(),
  price_precision: count,
  state: z.object({
    status: z.string(),
    feed_status: z.string(),
    source: z.string().nullable().default(null),
    expected_delay_seconds: z.number().nonnegative().default(0),
    market_closed: z.boolean().default(false),
  }),
});

export const instrumentsResponseSchema = z.object({
  generated_at: timestamp,
  instruments: z.array(instrumentRowSchema),
});

export type InstrumentRow = z.infer<typeof instrumentRowSchema>;
