import { z } from "zod";
import { figureSchema, playbookIndicatorSchema, readJson } from "../today/api";

// The Calendar page's week of release moments, built by the core
// (GET /api/v1/desk/calendar, core/desk/calendar.py). Results, surprise sizes,
// better/worse reads, NQ's 15-minute move, note links and playbook counts all
// come from there; the page only lays them out.

const time = z.string().min(1);

export const calendarFigureSchema = figureSchema.extend({
  reading: z.enum(["better", "worse"]).nullable(),
  reading_text: z.string().nullable(),
});

export const calendarRowSchema = z.object({
  at: time,
  title: z.string(),
  codes: z.array(z.string()),
  importance: z.enum(["critical", "high", "medium", "low"]),
  kind: z.enum(["economic", "fed", "speech"]),
  released: z.boolean(),
  figures: z.array(calendarFigureSchema),
  venue: z.string().nullable(),
  nq_15m: z.number().finite().nullable(),
  nq_15m_text: z.string().nullable(),
  nq_15m_status: z.enum(["upcoming", "waiting", "measured", "unavailable"]),
  note_id: z.string().min(1).nullable(),
  explainer_key: z.string().nullable(),
  explainer: z.string().nullable(),
  playbook_text: z.string().nullable(),
  playbook: playbookIndicatorSchema.nullable(),
});

export const calendarBoardSchema = z.object({
  generated_at: time,
  week: z.enum(["this", "next"]),
  start: time,
  end: time,
  tz: z.string().min(1),
  all: z.boolean(),
  hidden: z.number().int().nonnegative(),
  days: z.array(z.object({
    date: z.string(),
    label: z.string(),
    rows: z.array(calendarRowSchema),
  })),
  stale: z.boolean(),
  coverage: z.array(z.string()),
});

export type CalendarFigure = z.infer<typeof calendarFigureSchema>;
export type CalendarRow = z.infer<typeof calendarRowSchema>;
export type CalendarBoard = z.infer<typeof calendarBoardSchema>;
export type Week = CalendarBoard["week"];

export async function fetchCalendar(week: Week, all: boolean, tz: string, signal?: AbortSignal): Promise<CalendarBoard> {
  const query = new URLSearchParams({ week, all: String(all), tz });
  const response = await fetch(`/api/v1/desk/calendar?${query}`, { signal, cache: "no-store" });
  return calendarBoardSchema.parse(await readJson(response));
}
