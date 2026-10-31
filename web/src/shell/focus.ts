import { useEffect, useState } from "react";
import { z } from "zod";
import { chartInstrumentSchema, saveFocus, type ChartInstrument } from "../chart/api";
import { quoteSchema, readJson } from "../today/api";

// The global focus: the instrument the Chart page saves (NQ by default), which
// Today and the header lead with. The core picks the strip (focus first, then
// the watchlist) and its tiles; this file only fetches them and keeps the app's
// saves in order. Nothing here calls the desk, so a focus change writes no note.

/** NQ, used only when the core can't say what the focus is. */
export const NQ_FALLBACK: ChartInstrument = {
  symbol: "NQ", label: "Nasdaq 100 futures", unit: "index points", price_precision: 2, levels: true, release_pins: true,
};

export const focusStripSchema = z.object({
  generated_at: z.string().nullable(),
  focus: chartInstrumentSchema,
  watchlist_name: z.string(),
  symbols: z.array(z.string().min(1)),
  quotes: z.array(quoteSchema),
});
export type FocusStrip = z.infer<typeof focusStripSchema>;

export async function fetchFocusStrip(signal?: AbortSignal): Promise<FocusStrip> {
  const response = await fetch("/api/v1/focus/strip", { signal, cache: "no-store" });
  return focusStripSchema.parse(await readJson(response));
}

// One queue of focus saves for the whole app, not one per page visit, so a save
// started before leaving the Chart page can never land after a newer choice
// made when it is reopened. `pending` is the newest choice not yet saved.
export const focusSaves = { queue: Promise.resolve() as Promise<unknown>, pending: null as ChartInstrument | null, count: 0 };

const listeners = new Set<() => void>();

/** Called after every focus save settles (saved or not), so readers refetch. */
export function onFocusSaved(listener: () => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export function queueFocusSave(next: ChartInstrument): Promise<unknown> {
  focusSaves.pending = next;
  focusSaves.count += 1;
  const run = focusSaves.queue.then(() => saveFocus(next.symbol));
  focusSaves.queue = run.catch(() => undefined).finally(() => {
    focusSaves.count -= 1;
    if (focusSaves.count === 0) focusSaves.pending = null;
    listeners.forEach((listener) => listener());
  });
  return run;
}

export type FocusState = { data: FocusStrip | null; error: string | null };

// The focus strip, read after any saves in flight, every `everyMs` and again
// whenever a save settles. Only the newest request may land, so an answer for
// an older focus never replaces a newer one.
export function useFocusStrip(everyMs = 30_000): FocusState {
  const [state, setState] = useState<FocusState>({ data: null, error: null });
  useEffect(() => {
    let latest = 0;
    let controller: AbortController | null = null;
    const load = () => {
      const mine = ++latest;
      controller?.abort();
      const current = new AbortController();
      controller = current;
      focusSaves.queue.then(() => fetchFocusStrip(current.signal)).then(
        (data) => { if (mine === latest) setState({ data, error: null }); },
        (err: Error) => {
          if (mine !== latest || current.signal.aborted || err.name === "AbortError") return;
          setState((prev) => ({ ...prev, error: err.message || "Focus unavailable" }));
        });
    };
    load();
    const timer = setInterval(load, everyMs);
    const off = onFocusSaved(load);
    return () => { latest += 1; controller?.abort(); clearInterval(timer); off(); };
  }, [everyMs]);
  return state;
}
