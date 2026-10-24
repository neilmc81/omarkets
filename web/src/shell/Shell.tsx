import type { MouseEvent } from "react";
import type { FeedsStatus } from "./feeds";
import { PAGE_LABEL, PAGES, pageSearch, type Page } from "./routes";

export function ShellHeader({ page, onPage, feeds, statusOpen, onStatus, onExplain, canExplain, asking }: {
  page: Page;
  onPage: (page: Page) => void;
  feeds: FeedsStatus;
  statusOpen: boolean;
  onStatus: () => void;
  onExplain: () => void;
  canExplain: boolean;
  asking: boolean;
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
