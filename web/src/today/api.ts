import { z } from "zod";

const time = z.string().nullable();

export const figureSchema = z.object({
  name: z.string(),
  unit: z.string(),
  actual: z.number().nullable(),
  forecast: z.number().nullable(),
  previous: z.number().nullable(),
  decimals: z.number(),
  verdict: z.enum(["above", "below", "in_line", "not_reported", "no_forecast"]),
  actual_text: z.string(),
  forecast_text: z.string(),
  previous_text: z.string(),
  surprise: z.object({
    value: z.number(),
    value_text: z.string(),
    size: z.enum(["bigger", "typical", "smaller", "in_line", "not_enough_history"]),
    history_count: z.number(),
    larger_than: z.number().nullable(),
    text: z.string(),
  }).nullable().optional(),
});

const moveSchema = z.object({
  symbol: z.string(),
  label: z.string(),
  change: z.number(),
  change_unit: z.enum(["%", "bp"]),
  start_at: z.string(),
  end_at: z.string(),
  delayed: z.boolean(),
  text: z.string(),
});

const followUpSchema = z.object({
  checkpoints: z.array(z.object({
    label: z.enum(["1h", "close"]),
    at: z.string(),
    moves: z.array(moveSchema),
  })),
  verdict: z.enum(["held", "grew", "faded", "reversed", "quiet"]).nullable(),
  final: z.boolean(),
  text: z.string(),
});

export const quoteSchema = z.object({
  symbol: z.string(),
  label: z.string(),
  value: z.number().nullable(),
  value_text: z.string(),
  change_text: z.string().nullable(),
  direction: z.enum(["up", "down", "flat", "unknown"]),
  as_of: time,
  delayed: z.boolean(),
  stale: z.boolean(),
  // A cash index outside its hours (VIX): the value is its last close.
  closed: z.boolean().default(false),
  closed_text: z.string().nullable().default(null),
  explainer: z.string().nullable().default(null),
});

const yieldLineSchema = z.object({
  symbol: z.string(),
  label: z.string(),
  value: z.number().nullable(),
  value_text: z.string(),
  day_change_bp: z.number().nullable(),
  week_change_bp: z.number().nullable(),
  day_text: z.string(),
  week_text: z.string(),
});

const ratesSchema = z.object({
  as_of: time,
  lines: z.array(yieldLineSchema),
  curve_bp: z.number().nullable(),
  curve_text: z.string(),
  explainer: z.string(),
});

const headlineSchema = z.object({
  source: z.string(),
  title: z.string(),
  published_at: time,
  url: z.string().nullable(),
});

const upcomingSchema = z.object({
  kind: z.enum(["economic", "fed", "earnings", "auction"]),
  title: z.string(),
  at: time,
  when_text: z.string(),
  importance: z.enum(["critical", "high", "medium"]),
  detail: z.string().nullable(),
  explainer: z.string().nullable(),
});

const draftSchema = z.object({
  headline: z.string(),
  summary: z.string(),
  what_it_means: z.string(),
  market_reaction: z.string(),
  rates_read: z.string().nullish(),
  watch_next: z.array(z.string()),
  learn: z.string(),
  nq_tone: z.enum(["supportive", "negative", "mixed", "neutral"]),
});

export const noteSchema = z.object({
  id: z.string(),
  kind: z.enum(["primer", "premarket", "release", "fed", "earnings", "move", "ask", "review", "auction"]),
  title: z.string(),
  created_at: z.string(),
  event_at: time,
  figures: z.array(figureSchema),
  moves: z.array(moveSchema),
  headlines: z.array(headlineSchema),
  upcoming: z.array(upcomingSchema),
  rates: ratesSchema.nullable(),
  draft: draftSchema.nullable(),
  status: z.enum(["ok", "failed"]),
  error: z.string().nullable(),
  model: z.string().nullable(),
  cost_usd: z.number().nullable(),
  follow_up: followUpSchema.nullable().optional(),
  // The note's own instrument (null for desk-wide event notes) and the one its
  // tone is about; an older core sends neither.
  instrument: z.string().nullish(),
  tone_for: z.string().optional(),
});

const watchCardSchema = z.object({
  key: z.string(),
  at: z.string(),
  title: z.string(),
  importance: z.string(),
  explainer: z.string().nullable(),
  released: z.boolean(),
  figures: z.array(z.object({
    name: z.string(),
    forecast_text: z.string(),
    previous_text: z.string(),
    actual_text: z.string().nullable(),
    verdict: figureSchema.shape.verdict.nullable(),
    history: z.array(z.string()),
    history_note: z.string().nullable(),
  })),
  readings: z.array(z.object({
    family: z.enum(["inflation", "jobless", "growth", "fed"]),
    label: z.string(),
    above: z.string(),
    below: z.string(),
  })),
});

const nextEventSchema = z.object({
  at: z.string(),
  title: z.string(),
  importance: z.string(),
  kind: z.enum(["economic", "fed"]),
  explainer: z.string().nullable(),
  forecast_text: z.string().nullable(),
  playbook_text: z.string().nullable(),
});

export const deskSchema = z.object({
  generated_at: z.string(),
  session_text: z.string(),
  quotes: z.array(quoteSchema),
  rates: ratesSchema.nullable(),
  notes: z.array(noteSchema),
  upcoming: z.array(upcomingSchema),
  alerts: z.array(z.object({
    id: z.string(),
    rule: z.string(),
    label: z.string(),
    headline: z.string(),
    source: z.string(),
    url: z.string().nullable(),
    published_at: z.string(),
    created_at: z.string(),
  })).default([]),
  watch: z.array(watchCardSchema).default([]),
  next_event: nextEventSchema.nullable().default(null),
  writer_ready: z.boolean(),
  writer_detail: z.string().nullable(),
});

const learnEntrySchema = z.object({
  text: z.string(),
  note_id: z.string(),
  kind: noteSchema.shape.kind,
  title: z.string(),
  created_at: z.string(),
  times: z.number(),
  // Topic and heading from the core's fixed rules (core/desk/lessons.py).
  topic: z.string().nullable().default(null),
  topic_label: z.string().nullable().default(null),
  heading: z.string().nullable().default(null),
});

export const learnSchema = z.object({
  count: z.number(),
  entries: z.array(learnEntrySchema),
  topics: z.array(z.object({ key: z.string(), label: z.string(), count: z.number() })).default([]),
  most_taught: z.array(learnEntrySchema).default([]),
  taught: z.number().default(0),
});

const playbookReleaseSchema = z.object({
  at: z.string(),
  title: z.string(),
  figure: figureSchema,
  alongside: z.array(z.string()),
  nq_15m: moveSchema.nullable(),
  us2y_15m: moveSchema.nullable(),
  nq_close: moveSchema.nullable(),
  close_pending: z.boolean(),
  held: followUpSchema.shape.verdict,
  // The desk note written for this release, matched by the core; null when none.
  note_id: z.string().min(1).nullable(),
  // Actual minus forecast (the plot's x axis), from the core.
  surprise: z.number().finite().nullable().default(null),
  surprise_text: z.string().nullable().default(null),
});

export const playbookIndicatorSchema = z.object({
  name: z.string(),
  count: z.number(),
  enough: z.boolean(),
  text: z.string(),
  buckets: z.array(z.object({
    verdict: z.enum(["above", "below", "in_line"]),
    count: z.number(),
    text: z.string(),
    // NQ's average first-15-minute move (percent), from the core.
    nq_15m_avg: z.number().finite().nullable().default(null),
  })),
  releases: z.array(playbookReleaseSchema),
});

export const playbookSchema = z.object({
  generated_at: z.string(),
  since: time,
  min_releases: z.number(),
  indicators: z.array(playbookIndicatorSchema),
});

export type Desk = z.infer<typeof deskSchema>;
export type Playbook = z.infer<typeof playbookSchema>;
export type WatchCard = z.infer<typeof watchCardSchema>;
export type PlaybookRelease = z.infer<typeof playbookReleaseSchema>;
export type PlaybookIndicator = z.infer<typeof playbookIndicatorSchema>;
export type Learn = z.infer<typeof learnSchema>;
export type LearnEntry = z.infer<typeof learnEntrySchema>;
export type Note = z.infer<typeof noteSchema>;
export type Figure = z.infer<typeof figureSchema>;
export type FollowUp = z.infer<typeof followUpSchema>;
export type Quote = z.infer<typeof quoteSchema>;
export type Rates = z.infer<typeof ratesSchema>;
export type Upcoming = z.infer<typeof upcomingSchema>;
export type NextEvent = z.infer<typeof nextEventSchema>;

export async function readJson(response: Response): Promise<unknown> {
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
  return response.json();
}

export async function fetchDesk(signal?: AbortSignal): Promise<Desk> {
  const response = await fetch("/api/v1/desk", { signal, cache: "no-store" });
  return deskSchema.parse(await readJson(response));
}

export async function fetchNote(id: string, signal?: AbortSignal): Promise<Note> {
  const response = await fetch(`/api/v1/desk/notes/${encodeURIComponent(id)}`, { signal, cache: "no-store" });
  return noteSchema.parse(await readJson(response));
}

export async function fetchLearn(signal?: AbortSignal): Promise<Learn> {
  const response = await fetch("/api/v1/desk/learn", { signal, cache: "no-store" });
  return learnSchema.parse(await readJson(response));
}

export async function fetchPlaybook(signal?: AbortSignal): Promise<Playbook> {
  const response = await fetch("/api/v1/desk/playbook", { signal, cache: "no-store" });
  return playbookSchema.parse(await readJson(response));
}

// Explain the saved global focus right now. With no instrument the core reads
// its saved focus (NQ only when none is saved or it can't be read) and refuses
// (400, before any AI) a focus the desk does not write for.
export async function askDesk(): Promise<Note> {
  const response = await fetch("/api/v1/desk/ask", { method: "POST", cache: "no-store" });
  return noteSchema.parse(await readJson(response));
}
