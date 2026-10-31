import { z } from "zod";
import { requestValidated } from "../api/client";
import { watchlistSchema, type Watchlist } from "../chart/api";

// The Watchlists page reads the one watchlist (GET /api/v1/watchlist), the
// registry (GET /api/v1/chart/instruments) and the quotes the core already
// keeps for every instrument (GET /api/v1/instruments). Saving is one
// PUT /api/v1/watchlist: the core validates the symbols, writes no note and
// calls no AI.

const price = z.number().finite().nullable().default(null);

// The quote fields of /api/v1/instruments the page shows (the status panel's
// schema keeps only the feed fields; this one adds last and change).
export const quoteRowSchema = z.object({
  symbol: z.string().min(1),
  state: z.object({
    value: price,
    change: price,
    change_pct: price,
    timestamp: z.string().nullable().default(null),
    status: z.string(),
    feed_status: z.string(),
    source: z.string().nullable().default(null),
    expected_delay_seconds: z.number().nonnegative().default(0),
    market_closed: z.boolean().default(false),
  }),
});

export const quotesSchema = z.object({
  generated_at: z.string(),
  instruments: z.array(quoteRowSchema),
});

export type QuoteRow = z.infer<typeof quoteRowSchema>;
export type Quotes = z.infer<typeof quotesSchema>;

// The shared client's timeout and plain errors, with this page's schema.
export const fetchQuotes = (signal?: AbortSignal) => requestValidated("/api/v1/instruments", quotesSchema, signal);

/** A save the core refused; `message` is written for Neil, not a developer. */
export class WatchlistRejected extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "WatchlistRejected";
  }
}

type ValidationItem = { loc?: unknown; msg?: unknown };

/** FastAPI's 422 body (a string, or pydantic's list of errors) as one plain sentence. */
export function rejectionText(detail: unknown): string | null {
  if (typeof detail === "string") return detail;
  if (!Array.isArray(detail)) return null;
  const parts = (detail as ValidationItem[]).map((item) => {
    const msg = typeof item.msg === "string" ? item.msg.replace(/^Value error, /, "") : null;
    if (!msg) return null;
    const field = Array.isArray(item.loc) ? item.loc.filter((p) => p !== "body").join(" ") : "";
    return field === "name" ? `Name: ${msg}` : msg;
  }).filter((p): p is string => Boolean(p));
  return parts.length ? [...new Set(parts)].join(" · ") : null;
}

export async function saveWatchlist(next: { name: string; symbols: string[] }): Promise<Watchlist> {
  const response = await fetch("/api/v1/watchlist", {
    method: "PUT", cache: "no-store", headers: { "Content-Type": "application/json" }, body: JSON.stringify(next),
  });
  if (!response.ok) {
    let text: string | null = null;
    try {
      text = rejectionText(((await response.json()) as { detail?: unknown }).detail);
    } catch {
      // keep the status text
    }
    throw new WatchlistRejected(text ?? `HTTP ${response.status}`, response.status);
  }
  return watchlistSchema.parse(await response.json());
}
