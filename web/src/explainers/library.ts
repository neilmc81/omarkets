import { useEffect, useState } from "react";
import { z } from "zod";

// The explainer library: fixed plain-English hover texts written once in the
// core (core/desk/explainers.py, GET /api/v1/explainers). No AI, no market
// logic here: the web app looks entries up by key and shows them.

export const explainerSchema = z.object({
  key: z.string().min(1),
  kind: z.enum(["instrument", "indicator", "level", "segment", "reading", "topic"]),
  name: z.string().min(1),
  title: z.string(),
  what: z.string(),
  why_nq: z.string(),
  rising: z.string().nullable(),
  falling: z.string().nullable(),
  rising_label: z.string().nullable(),
  falling_label: z.string().nullable(),
});

export const explainerLibrarySchema = z.object({ entries: z.array(explainerSchema) });

export type Explainer = z.infer<typeof explainerSchema>;
export type ExplainerLibrary = ReadonlyMap<string, Explainer>;

export async function fetchExplainers(signal?: AbortSignal): Promise<ExplainerLibrary> {
  const response = await fetch("/api/v1/explainers", { signal });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const { entries } = explainerLibrarySchema.parse(await response.json());
  return new Map(entries.map((entry) => [entry.key, entry]));
}

/** The hover-card lines for an entry: what it is, why it matters, rising and falling. */
export function explainerLines(entry: Explainer): string[] {
  return [
    entry.what,
    `Why it matters for NQ: ${entry.why_nq}`,
    ...(entry.rising ? [`${entry.rising_label ?? "Rising"}: ${entry.rising}`] : []),
    ...(entry.falling ? [`${entry.falling_label ?? "Falling"}: ${entry.falling}`] : []),
  ];
}

// The library is fixed while the core runs, so it is fetched once per page load.
let loading: Promise<ExplainerLibrary> | null = null;

/** Forget the cached library (tests). */
export function resetExplainers(): void {
  loading = null;
}

/** The library, or null until it loads (or if it cannot be loaded: callers fall back). */
export function useExplainers(): ExplainerLibrary | null {
  const [library, setLibrary] = useState<ExplainerLibrary | null>(null);
  useEffect(() => {
    let live = true;
    loading ??= fetchExplainers();
    loading.then((value) => { if (live) setLibrary(value); }, () => { loading = null; });
    return () => { live = false; };
  }, []);
  return library;
}
