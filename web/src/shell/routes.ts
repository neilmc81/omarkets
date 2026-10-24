export const PAGES = ["today", "chart", "calendar", "macro", "news", "playbook", "learn"] as const;
export type Page = typeof PAGES[number];

export const PAGE_LABEL: Record<Page, string> = {
  today: "Today",
  chart: "Chart",
  calendar: "Calendar",
  macro: "Macro",
  news: "News",
  playbook: "Playbook",
  learn: "Learn",
};

// Old deep links. The native Omarchy layer still opens `?section=...`, and
// `?view=details` was the old workstation; its Overview section held the NQ
// chart. Retired sections (analysts, briefings, history, technical) and
// anything unknown open Today, where the notes live.
const LEGACY: Record<string, Page> = {
  overview: "chart",
  details: "chart",
  macro: "macro",
  calendar: "calendar",
  news: "news",
};

const isPage = (value: string): value is Page => (PAGES as readonly string[]).includes(value);

export function pageFromLocation(search = window.location.search): Page {
  const params = new URLSearchParams(search);
  const view = params.get("view")?.toLowerCase();
  if (view && isPage(view)) return view;
  if (view && LEGACY[view]) return LEGACY[view];
  const section = params.get("section")?.toLowerCase();
  return section && LEGACY[section] ? LEGACY[section] : "today";
}

export function pageSearch(page: Page): string {
  return page === "today" ? "" : `?view=${page}`;
}
