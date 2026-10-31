import { useCallback, useEffect, useState } from "react";
import { CalendarPage } from "./calendar/CalendarPage";
import { ChartPage } from "./chart/ChartPage";
import { LearnPage } from "./learn/LearnPage";
import { MacroPage } from "./macro/MacroPage";
import { NewsPage } from "./newsboard/NewsPage";
import { PlaybookPage } from "./playbook/PlaybookPage";
import { askDesk } from "./today/api";
import { Today } from "./today/Today";
import { NavigateContext } from "./shell/Explain";
import { feedsStatus } from "./shell/feeds";
import { useFocusStrip } from "./shell/focus";
import { pageFromLocation, pageSearch, PAGES, type Page } from "./shell/routes";
import { ShellHeader } from "./shell/Shell";
import { StatusPanel } from "./shell/StatusPanel";
import { useDesk } from "./shell/useDesk";
import { useShortcuts } from "./shell/useShortcuts";


const noteFromLocation = (search = window.location.search) => new URLSearchParams(search).get("note");

export function Root() {
  const [page, setPage] = useState<Page>(() => pageFromLocation());
  const [openId, setOpenId] = useState<string | null>(() => noteFromLocation());
  const [asking, setAsking] = useState(false);
  const [askError, setAskError] = useState<string | null>(null);
  const [statusOpen, setStatusOpen] = useState(false);
  const closeStatus = useCallback(() => setStatusOpen(false), []);
  const { desk, error, health, healthError, reload } = useDesk();
  // The global focus (NQ by default): Today's market section and the header follow it.
  const focus = useFocusStrip();

  const go = useCallback((next: Page, note?: string | null) => {
    const search = note ? `?note=${encodeURIComponent(note)}` : pageSearch(next);
    if (window.location.search !== search) {
      const url = new URL(window.location.href);
      url.search = search;
      window.history.pushState(null, "", url);
    }
    setPage(next);
    if (note) setOpenId(note);
  }, []);
  const navigate = useCallback((search: string) => go(pageFromLocation(search), noteFromLocation(search)), [go]);

  useEffect(() => {
    const onPop = () => {
      setPage(pageFromLocation());
      const note = noteFromLocation();
      if (note) setOpenId(note);
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const canExplain = Boolean(desk?.writer_ready);
  const feeds = feedsStatus({ desk, deskError: error, health, healthError, quotes: focus.data?.quotes });
  const ask = async () => {
    if (asking || !canExplain) return;
    go("today");
    setAsking(true);
    setAskError(null);
    try {
      const note = await askDesk();
      setOpenId(note.id);
      await reload();
    } catch (err) {
      setAskError((err as Error).message || "Could not write a note");
    } finally {
      setAsking(false);
    }
  };

  useShortcuts({
    e: () => void ask(),
    ...Object.fromEntries(PAGES.map((item, index) => [String(index + 1), () => go(item)])),
  });

  return <NavigateContext.Provider value={navigate}>
    <div className="today shell">
      <ShellHeader page={page} onPage={go} onExplain={() => void ask()} canExplain={canExplain} asking={asking}
        feeds={feeds} focus={focus.data} statusOpen={statusOpen} onStatus={() => setStatusOpen((open) => !open)} />
      {statusOpen && <StatusPanel feeds={feeds} health={health} healthError={healthError} desk={desk} deskError={error} onClose={closeStatus} />}
      {page === "today" && <Today desk={desk} error={askError ?? error} openId={openId} onOpen={setOpenId} focus={focus} />}
      {page === "chart" && <ChartPage />}
      {page === "calendar" && <CalendarPage />}
      {page === "macro" && <MacroPage />}
      {page === "news" && <NewsPage />}
      {page === "learn" && <LearnPage />}
      {page === "playbook" && <PlaybookPage />}
      <footer className="t-footer">Education and analysis only · no trading or order execution</footer>
    </div>
  </NavigateContext.Provider>;
}
