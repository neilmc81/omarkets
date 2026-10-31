import type { MouseEvent } from "react";
import type { FeedsStatus } from "./feeds";
import type { FocusStrip } from "./focus";
import { PAGE_LABEL, PAGES, pageSearch, type Page } from "./routes";

export function ShellHeader({ page, onPage, feeds, statusOpen, onStatus, onExplain, canExplain, asking, focus = null }: {
  page: Page;
  onPage: (page: Page) => void;
  feeds: FeedsStatus;
  statusOpen: boolean;
  onStatus: () => void;
  onExplain: () => void;
  canExplain: boolean;
  asking: boolean;
  focus?: FocusStrip | null;
}) {
  const follow = (next: Page) => (event: MouseEvent) => {
    if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey) return;
    event.preventDefault();
    onPage(next);
  };
  return <header className="shell-header">
    <a className="shell-wordmark" href="?" onClick={follow("today")} aria-label="Omarkets home"><span>O</span>markets</a>
    <nav className="shell-tabs" aria-label="Pages">
      {PAGES.map((item, index) => <a key={item} href={pageSearch(item) || "?"} onClick={follow(item)}
        aria-current={page === item ? "page" : undefined} aria-keyshortcuts={String(index + 1)} title={`${PAGE_LABEL[item]} (${index + 1})`}>
        {PAGE_LABEL[item]}
      </a>)}
    </nav>
    <div className="shell-end">
      {focus && <FocusChip strip={focus} onOpen={follow("chart")} />}
      <button type="button" className={`shell-feeds tone-${feeds.tone}`} aria-expanded={statusOpen} onClick={onStatus}
        title="System status: feeds, sources and the AI writer">
        <i className="shell-dot" aria-hidden="true" />{feeds.label}
      </button>
      <button className="t-primary" onClick={onExplain} disabled={!canExplain || asking} aria-keyshortcuts="E">
        {asking ? "Writing…" : "Explain the market now (E)"}
      </button>
    </div>
  </header>;
}

// The global focus instrument's quote, from the core's focus strip. It links to
// the Chart page, where the focus is chosen.
function FocusChip({ strip, onOpen }: { strip: FocusStrip; onOpen: (event: MouseEvent) => void }) {
  const { symbol, label } = strip.focus;
  const quote = strip.quotes.find((q) => q.symbol === symbol);
  const change = !quote ? null : quote.closed ? quote.closed_text ?? "closed" : quote.stale ? "stale" : quote.change_text;
  return <a className="shell-focus" href={pageSearch("chart")} onClick={onOpen} aria-label={`Focus: ${label}`}
    title={`Focus: ${label}${quote?.delayed ? " (delayed)" : ""}. Today and the Omarchy bar follow it; change it on the Chart page.`}>
    <b>{symbol}</b>
    <span>{quote ? `${quote.value_text}${quote.delayed ? "*" : ""}` : "unavailable"}</span>
    {change && <span className={`t-chg ${quote && !quote.closed && !quote.stale ? quote.direction : ""}`}>{change}</span>}
  </a>;
}
