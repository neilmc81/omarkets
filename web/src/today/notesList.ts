import { useEffect, useState } from "react";

// How the Notes list is laid out: New York days (display only; the core keeps
// UTC) and which notes arrived since the user last looked at Today.

export type DayGroup = "today" | "yesterday" | "earlier";

export const GROUP_LABEL: Record<DayGroup, string> = { today: "Today", yesterday: "Yesterday", earlier: "Earlier" };

const nyParts = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit" });

/** The New York calendar day of a moment, as YYYY-MM-DD. */
export function nyDay(at: number | string): string {
  const parts = Object.fromEntries(nyParts.formatToParts(new Date(at)).map((p) => [p.type, p.value]));
  return `${parts.year}-${parts.month}-${parts.day}`;
}

function dayBefore(day: string): string {
  const date = new Date(`${day}T12:00:00Z`);
  date.setUTCDate(date.getUTCDate() - 1);
  return date.toISOString().slice(0, 10);
}

export type NoteGroup<T> = { key: DayGroup; notes: T[] };

/** Notes newest first, split into Today / Yesterday / Earlier by New York day. Empty groups are left out. */
export function groupNotes<T extends { created_at: string }>(notes: T[], now: number): NoteGroup<T>[] {
  const today = nyDay(now);
  const yesterday = dayBefore(today);
  const groups: Record<DayGroup, T[]> = { today: [], yesterday: [], earlier: [] };
  const sorted = [...notes].sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));
  for (const note of sorted) {
    const day = nyDay(note.created_at);
    groups[day >= today ? "today" : day === yesterday ? "yesterday" : "earlier"].push(note);
  }
  return (Object.keys(groups) as DayGroup[]).filter((key) => groups[key].length > 0).map((key) => ({ key, notes: groups[key] }));
}

/** Ids of notes written after the last visit. Nothing is "new" on a first visit. */
export function newSince<T extends { id: string; created_at: string }>(notes: T[], seenBefore: number | null): Set<string> {
  if (seenBefore === null) return new Set();
  return new Set(notes.filter((n) => Date.parse(n.created_at) > seenBefore).map((n) => n.id));
}

export const LAST_SEEN_KEY = "marketos.notes.lastSeen"; // pre-rename key, kept so read notes stay read

function storage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null; // storage disabled
  }
}

export function readLastSeen(): number | null {
  try {
    const value = storage()?.getItem(LAST_SEEN_KEY);
    const at = value ? Date.parse(value) : NaN;
    return Number.isFinite(at) ? at : null;
  } catch {
    return null;
  }
}

export function writeLastSeen(at: number): void {
  try {
    storage()?.setItem(LAST_SEEN_KEY, new Date(at).toISOString());
  } catch {
    // full or blocked storage: the divider just won't remember this visit
  }
}

/**
 * When the user last looked at Today, before this visit. A visit starts when
 * Today mounts or the window comes back into view; the stored time moves on
 * then and when the user leaves, never on a poll, so the divider holds still
 * while they read.
 */
export function useLastSeen(): number | null {
  const [seenBefore, setSeenBefore] = useState<number | null>(readLastSeen);
  useEffect(() => {
    writeLastSeen(Date.now());
    const onVisibility = () => {
      if (document.visibilityState === "visible") setSeenBefore(readLastSeen());
      writeLastSeen(Date.now());
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      writeLastSeen(Date.now());
    };
  }, []);
  return seenBefore;
}
