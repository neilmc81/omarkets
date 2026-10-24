import { z } from "zod";
import { readJson } from "../today/api";
import { calendarFigureSchema } from "../calendar/api";

// The Macro page's cards, built by the core (GET /api/v1/desk/macro,
// core/desk/macro.py). Values, trends, last-release reads and whether a rise is
// good or bad for NQ all come from there; the page only lays them out.

const effect = z.enum(["good", "bad", "mixed", "neutral"]);

export const macroCardSchema = z.object({
  key: z.string(),
  group: z.enum(["inflation", "jobs", "growth", "fed"]),
  title: z.string(),
  subtitle: z.string().nullable(),
  value: z.number().finite().nullable(),
  value_text: z.string(),
  period_text: z.string().nullable(),
  at: z.string().nullable(),
  trend: z.enum(["rising", "falling", "steady", "unknown"]),
  trend_text: z.string(),
  nq_when_rising: z.enum(["bad", "good", "mixed"]),
  nq_text: z.string(),
  nq_effect: effect,
  points: z.array(z.number().finite()),
  release: z.object({
    at: z.string(),
    figure: calendarFigureSchema,
    text: z.string(),
    nq_effect: effect,
  }).nullable(),
  explainer_key: z.string().nullable(),
  sources: z.array(z.string()),
  stale: z.boolean(),
  quote_symbol: z.string().nullable(),
});

export const macroBoardSchema = z.object({
  generated_at: z.string(),
  groups: z.array(z.object({
    key: macroCardSchema.shape.group,
    title: z.string(),
    cards: z.array(macroCardSchema),
  })),
});

export type MacroCard = z.infer<typeof macroCardSchema>;
export type MacroBoard = z.infer<typeof macroBoardSchema>;

export async function fetchMacro(signal?: AbortSignal): Promise<MacroBoard> {
  const response = await fetch("/api/v1/desk/macro", { signal, cache: "no-store" });
  return macroBoardSchema.parse(await readJson(response));
}
