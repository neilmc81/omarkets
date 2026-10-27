import { useEffect, useRef } from "react";
import { fetchInstruments } from "../api/client";
import type { HealthResponse, InstrumentRow } from "../api/schemas";
import { usePoll } from "../chart/usePoll";
import type { Desk } from "../today/api";
import type { FeedsStatus } from "./feeds";

// The small status panel behind the feeds dot: what the core reports about
// itself and its sources. It replaces the old workstation's diagnostics and
// judges nothing about the market.

const ET = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
const at = (iso: string | null | undefined) => (iso ? `${ET.format(new Date(iso))} ET` : "—");

const SOURCE_STATUS: Record<string, string> = {
  healthy: "OK", degraded: "degraded", unavailable: "unavailable", not_configured: "not set up",
};

// What the feed is doing now, in the words the rest of the app uses.
function feedNow(row: InstrumentRow): string {
  const { status, feed_status: feed, market_closed: closed } = row.state;
  if (status === "unavailable") return "unavailable";
  if (closed) return "market closed";
  if (status === "stale") return feed === "delayed" ? "delayed · stale" : "stale";
  return feed;
}

const delayText = (row: InstrumentRow) => row.state.status === "unavailable" ? "—"
  : row.state.expected_delay_seconds > 0 ? `${Math.round(row.state.expected_delay_seconds / 60)} min delay` : "real time";

// Read-only: every instrument the core knows, where it trades and which
// calendar decides when its market is open. Nothing here can be edited. It
// refreshes while the panel is open, so "Now" follows the core.
const INSTRUMENTS_POLL_MS = 15_000;

function InstrumentsTable() {
  const { data, error } = usePoll(fetchInstruments, "instruments", INSTRUMENTS_POLL_MS);
  if (!data) return error ? <p className="t-error" role="status">Instruments unavailable: {error}</p>
    : <p className="t-muted">Loading…</p>;
  const rows = data.instruments;
  return <>
  <p className="t-muted">As of {at(data.generated_at)}{error ? ` · refresh failed: ${error}` : ""}</p>
  <table className="status-table instruments-table" aria-label="Instruments">
    <thead><tr><th>Symbol</th><th>Class</th><th>Venue</th><th>Currency</th><th>Calendar</th><th>Decimals</th><th>Source</th><th>Now</th></tr></thead>
    <tbody>{rows.map((row) => <tr key={row.symbol}>
      <td title={row.display_name}><b>{row.symbol}</b></td>
      <td>{row.asset_class.replaceAll("_", " ")}</td>
      <td>{row.venue}</td>
      <td>{row.currency ?? "—"}</td>
      <td>{row.session_calendar_label}</td>
      <td>{row.price_precision}</td>
      <td>{row.state.source ?? "—"} · {delayText(row)}</td>
      <td>{feedNow(row)}</td>
    </tr>)}</tbody>
  </table>
  </>;
}

export function StatusPanel({ feeds, health, healthError, desk, deskError, onClose }: {
  feeds: FeedsStatus;
  health: HealthResponse | null;
  healthError: string | null;
  desk: Desk | null;
  deskError: string | null;
  onClose: () => void;
}) {
  const ref = useRef<HTMLElement | null>(null);
  useEffect(() => {
    ref.current?.focus();
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const delayed = desk?.quotes.filter((q) => q.delayed).map((q) => q.label) ?? [];
  const stale = desk?.quotes.filter((q) => q.stale && !q.closed).map((q) => q.label) ?? [];
  return <section ref={ref} tabIndex={-1} className="status-panel" role="dialog" aria-label="System status">
    <header className="status-head">
      <h2>System status</h2>
      <button type="button" className="t-link" aria-label="Close the status panel" onClick={onClose}>✕</button>
    </header>
    <ul className="status-lines">{feeds.lines.map((line) => <li key={line}>{line}</li>)}</ul>
    <h3>Omarkets core</h3>
    {healthError && <p className="t-error" role="status">Health check failed: {healthError}{health ? " · last known values below" : ""}</p>}
    {health ? <dl className="status-grid">
      <dt>Core</dt><dd>{health.status} · version {health.version} · checked {at(health.time)}</dd>
      <dt>Database</dt><dd>{health.database_ok ? "OK" : "Error"}</dd>
      <dt>Quote feed</dt><dd>{health.provider} · {health.provider_status}{health.provider_error ? ` · ${health.provider_error}` : ""}</dd>
      <dt>Quote stream</dt><dd>{health.stream_running ? "running" : "stopped"} · last update {at(health.last_update_at)}</dd>
      <dt>Markets cached</dt><dd>{health.cached_instruments}</dd>
      {health.news && <><dt>News sources</dt><dd>{health.news.enabled ? `${health.news.healthy_sources} of ${health.news.source_count} healthy · ${health.news.degraded_sources} degraded · ${health.news.offline_sources} offline` : "off"}</dd></>}
    </dl> : !healthError && <p className="t-muted">Checking…</p>}
    <h3>Desk</h3>
    {deskError && <p className="t-error" role="status">Desk: {deskError}</p>}
    {desk && <dl className="status-grid">
      <dt>AI writer</dt><dd>{desk.writer_ready ? "ready" : `not available${desk.writer_detail ? ` · ${desk.writer_detail}` : ""}`}</dd>
      <dt>Delayed quotes</dt><dd>{delayed.length ? delayed.join(", ") : "none"}</dd>
      <dt>Stale quotes</dt><dd>{stale.length ? stale.join(", ") : "none"}</dd>
      <dt>Desk read</dt><dd>{at(desk.generated_at)}</dd>
    </dl>}
    {health && health.economic_providers.length > 0 && <>
      <h3>Economic sources</h3>
      <table className="status-table">
        <thead><tr><th>Source</th><th>Status</th><th>Checked</th></tr></thead>
        <tbody>{health.economic_providers.map((p) => <tr key={p.name} className={`st-${p.status}`}>
          <td>{p.name.replaceAll("_", " ")}</td>
          <td>{SOURCE_STATUS[p.status] ?? p.status}{p.detail ? ` · ${p.detail}` : ""}</td>
          <td>{at(p.checked_at)}</td>
        </tr>)}</tbody>
      </table>
    </>}
    <h3>Instruments</h3>
    <InstrumentsTable />
    <h3>About</h3>
    <div className="status-about">
      <p>Omarkets is open source under the MIT licence. It is not financial advice and never places orders. Quotes are delayed and may be wrong or late.</p>
      <p>Charts by <a href="https://www.tradingview.com/" target="_blank" rel="noreferrer">TradingView</a> Lightweight Charts.</p>
      <p>This product uses the FRED® API but is not endorsed or certified by the Federal Reserve Bank of St. Louis.</p>
      <p>This product uses the Census Bureau Data API but is not endorsed or certified by the Census Bureau.</p>
      <p>BLS.gov cannot vouch for the data or analyses derived from these data after the data have been retrieved from BLS.gov.</p>
    </div>
  </section>;
}
