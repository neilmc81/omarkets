import { z } from "zod";
import { readJson } from "../today/api";

// The News page's headlines, tagged by the core's fixed rules
// (GET /api/v1/desk/news, core/desk/headlines.py): category, scheduled or not,
// and NQ's move in the next 15 minutes. The page only filters and lays them out.

export const CATEGORIES = ["fed", "economy", "big_tech", "energy", "geopolitics"] as const;

const time = z.string().min(1);

export const newsItemSchema = z.object({
  id: z.string(),
  at: time,
  source: z.string(),
  source_label: z.string(),
  headline: z.string(),
  url: z.string().nullable(),
  category: z.enum(CATEGORIES).nullable(),
  category_label: z.string().nullable(),
  scheduled: z.boolean(),
  nq_15m: z.number().finite().nullable(),
  nq_15m_text: z.string().nullable(),
  nq_15m_status: z.enum(["unmeasured", "waiting", "measured", "unavailable"]),
  alert: z.boolean(),
});

export const newsBoardSchema = z.object({
  generated_at: time,
  start: time,
  items: z.array(newsItemSchema),
  hidden: z.number().int().nonnegative(),
  alerts: z.array(z.object({
    id: z.string(),
    rule: z.string(),
    label: z.string(),
    headline: z.string(),
    source: z.string(),
    url: z.string().nullable(),
    published_at: time,
    created_at: time,
  })),
});

export type NewsItem = z.infer<typeof newsItemSchema>;
export type NewsBoard = z.infer<typeof newsBoardSchema>;
export type NewsCategory = typeof CATEGORIES[number];

export async function fetchNewsBoard(signal?: AbortSignal): Promise<NewsBoard> {
  const response = await fetch("/api/v1/desk/news", { signal, cache: "no-store" });
  return newsBoardSchema.parse(await readJson(response));
}
