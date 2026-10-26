# Omarkets reference

> The detailed operator and developer reference that used to be the README. Sections on
> Phases 6–12, the Qwen voice interface and older modules are partly historical; the
> current design is [the desk](desk.md). The project overview is in the [README](../README.md).

A plain-English market desk for a beginner Nasdaq 100 futures (NQ) trader,
built on Omarchy Linux.

Omarkets ingests market and macro information deterministically, computes every
number in Python, and asks an AI writer only to explain those facts in plain
English. Results surface on the **Today** screen, in a thin Omarchy popup and as
desktop notifications.

**Omarkets is not a trading bot. It does not place orders.**

**Start here: [the desk design](desk.md).** The default screen is **Today**:
quotes, a bonds & rates panel, upcoming data/Fed/earnings, and notes written at
09:00 ET (after the 08:30 data), about 26 minutes after every economic release
(15 minutes of reaction + the 10-minute feed delay), after Fed events and
mega-cap earnings, after notably strong or weak Treasury auctions, when NQ
makes a big move, a week-in-review after Friday's close, and on request. Notes
show "did it hold?" follow-ups and surprise sizes; a Learn library collects every
lesson; Coming up includes Treasury auctions and Fed speakers; quiet keyword
rules raise breaking-news alerts without AI. **Details** keeps the NQ chart,
macro series, economic calendar and news feed.

On October 20, 2026 the older Jev decision engine, analyst service, briefings,
workflows, historical/research services and technical-analysis code were
removed (see [what was removed](desk.md#what-was-removed-october-20-2026)).
The gate/repair documents under `docs/` are historical.

Primary focus: **Nasdaq futures (NQ)**, with cross-asset context (ES, US 2Y/10Y
yields, DXY, VIX/VXN, major Nasdaq tech and semiconductor names, crude oil,
gold, Bitcoin).

## Status

Real market quotes come from free feeds: NQ/ES futures (explicit front-month
contract), Treasury yields, DXY, VIX/VXN, commodities and equities via CNBC, and
BTC via Binance. **NQ/ES are about 10 minutes delayed** and labelled `DELAYED`,
never `LIVE`; yields and the dollar are live. Every instrument reports value,
change, source, contract, exchange/received timestamps, freshness and an
explicit LIVE / DELAYED / STALE / UNAVAILABLE / DEMO status. Demo mode uses the
synthetic provider and is never an automatic fallback.

Real macro observations and a public-source economic calendar with market
consensus are operational (consensus is a third-party estimate, not an official
statistic). News, central-bank and geopolitical headlines are ingested from
public/official feeds, normalized and deduplicated. The desk writes one note per
moment that matters, through one OpenRouter call reserved and settled by the AI
cost guard ($10/week). The Omarchy plugin adds a bar label, a popup with the
latest note, and hotkeys; a read-only MCP adapter lets the Qwen voice assistant
read Omarkets. No trading or order execution is implemented.

## Quick start

Requires Python 3.12+ (developed on 3.14) and Node 22.13+, 24+, or 26+
(developed on 26.8) for the frontend tooling. Backend commands below run from
the repository root; frontend commands run from `web/`.

```bash
# 1. Create a virtual environment and install (runtime + dev tools)
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# 2. Run the core (binds 127.0.0.1:8765 by default)
.venv/bin/omarkets

# 3. In another shell, inspect it
.venv/bin/omarketsctl status
.venv/bin/omarketsctl state
.venv/bin/omarketsctl state NQ
# Settings the running service really uses, and where each came from
# (code default, .env or systemd drop-in). Secrets show only set/not set.
.venv/bin/omarketsctl config          # add --all for every setting
# Stored desk-note costs by model and prompt version (read-only)
.venv/bin/omarketsctl note-costs

# 4. Run the backend tests
.venv/bin/pytest

# 5. Lint / format
.venv/bin/ruff check core tests
.venv/bin/ruff format --check core tests
```

If `uv` is available, the equivalent is `uv venv && uv pip install -e ".[dev]"`
and `uv run omarkets` / `uv run pytest`. The project does not require `uv`.

### Frontend (web app)

```bash
cd web
npm install

# Development (proxies /api, /health and /ws to 127.0.0.1:8765)
npm run dev            # http://127.0.0.1:5173

# Tests, type check, lint, production build
npm test
npm run typecheck
npm run lint
npm run build          # outputs web/dist

# Serve the production build locally (same proxying as dev)
npm run preview        # http://127.0.0.1:4173
```

The frontend keeps the backend as the single source of truth: it never
recomputes values, changes, or staleness, and it displays
`unavailable`/`stale` exactly as the core reports them.

### Omarchy launch

`SUPER + M` starts the backend and frontend if they are not already running,
then opens the workstation (built preview on `4173`). It is safe to press
repeatedly: each server is started only when its loopback port is idle.

```bash
./omarchy/omarkets-start              # start servers + open dev server (5173)
./omarchy/omarkets-start 4173         # start servers + open production preview
./omarchy/omarkets-stop               # stop the servers this user started
./omarchy/omarkets-launch 4173        # open the UI only (servers must be running)
./omarchy/install-desktop 4173        # optional per-user app-menu entry
```

`omarkets-start` resolves `npm` even when it lives behind a version manager,
waits for each port to become ready, and writes logs to
`~/.local/state/omarkets/`. The launcher uses `omarchy launch webapp` when
available and falls back to `xdg-open`. The optional installer refuses to
overwrite an existing entry. Nothing is installed automatically, and the
repository must remain in place.

The hotkey is a single line in `~/.config/hypr/bindings.lua`:

```lua
o.bind("SUPER + M", "Omarkets",
       "/path/to/omarkets/omarchy/omarkets-start 4173")
```

Use the absolute path of your checkout. `SUPER + M` was free;
`SUPER + SHIFT + M` remains bound to Music. Preview requires `npm run build`
to have been run at least once.

### Frontend transport and chart limitations

React hooks own one WebSocket and non-overlapping REST state/health polling.
Zod validates responses; malformed data retains the last valid snapshot with
an error. Reconnect delays grow from 1 to 15 seconds; offline/unmount cancels
requests and timers. REST refreshes backend freshness even on a quiet stream.
Disconnected values and cached health are qualified as last-known.

The NQ chart (`web/src/chart/SessionChart.tsx`, TradingView Lightweight Charts
v5) draws OHLC bars, the session calendar, key levels, release pins and % overlays
exactly as the core serves them (`/api/v1/bars`, `/session`, `/levels`,
`/chart/pins`; see "Chart API" below). Gap bars are drawn as whitespace, never as
zero candles. Times on the axis and in hover cards are New York time; cards also
give "your time". The chart polls (1D every minute, 5D every two minutes, 1M every
five); it is not a tick-by-tick stream. The old raw 1,000-observation line
(`HistoryChart`) and the old workstation pages are gone (UI redesign Phase 6);
core and source diagnostics now sit in the status panel behind the header's
feeds dot.

Dev and preview bind only to `127.0.0.1`, with same-origin REST/WebSocket
proxies; no backend CORS changes are needed. Set `OMARKETS_BACKEND_TARGET` to
another `http://127.0.0.1:<port>` origin if the backend port changes. Preview
serves the built files locally, not as a public production hosting solution.

## Phase 2 economics

Launch commands are unchanged. Install the updated Python dependencies first:
`.venv/bin/pip install -e ".[dev]"`. `omarkets` defaults to live economics;
first provider checks run asynchronously, so panels may initially be empty.
No key is needed for BLS, NY Fed, BEA schedules, or Fed calendars/notices.

### Sources and actual coverage

These five public adapters were fetched successfully during implementation and
again through the running application on September 17 UTC / September 18 local,
2026. Availability is not guaranteed; inspect provider health in the UI.

| Provider | Structured source | Available data | Default checks |
| --- | --- | --- | --- |
| `bls` | BLS public v2 timeseries API, batched POST | CPI/core CPI MoM and YoY; payroll change; unemployment; AHE MoM/YoY; JOLTS | 24 hours |
| `nyfed` | NY Fed Markets API reference rates JSON | EFFR, SOFR, effective-date target lower/upper bounds | 1 hour |
| `bea_calendar` | BEA official ICS subscription | GDP estimate schedules, Personal Income and Outlays/PCE, other BEA releases | 6 hours |
| `bea_releases` | BEA news release RSS | Published BEA release notices (GDP, PCE/personal income, trade, etc.) with real publication times | 6 hours |
| `fed_calendar` | Federal Reserve calendar JSON | FOMC decision slots, press conferences/minutes, Beige Book, industrial production schedules | 6 hours |
| `fed_releases` | Federal Reserve monetary-policy RSS | Published FOMC statements and SEP notices; no numeric extraction | 30 minutes |
| `fred` | Authenticated FRED observations API | PCE/core PCE YoY, GDP, retail sales, industrial production, claims, PPI/core PPI, housing starts, permits, new home sales, durable goods, Empire/Philly Fed | 6 hours |
| `consensus` | ForexFactory weekly calendar JSON | US release schedule with **market consensus/forecast** and previous; ISM/ADP where listed | 1 hour |

FRED is operational with a configured key: **28 macro series live**. Housing,
durables, and the ISM substitutes come from FRED (`HOUST`, `PERMIT`, `HSN1F`,
`DGORDER`, `GACDISA066MSFRBNY`, `GACDFSA066MSFRBPHI`). The Census API key
(free, from api.census.gov/data/key_signup.html) is needed only for the
`census_retail` headline; without it that provider reports `not_configured` and
sends no request (census.gov web pages answer 403 to programs). A BLS registered key is optional; the keyless
BLS query works, and an invalid key falls back automatically rather than failing.
**EIA is stored but unused — energy is Phase 3 market scope.** No ISM license is
purchased; regional Fed surveys stand in for headline ISM.

ForexFactory provides a real **market consensus** and real scheduled times,
including release families BLS/Census calendars block (CPI, NFP, claims, housing,
ISM). Consensus is a third-party market estimate, not an official statistic, and
is labeled as such. Actuals populate for released events when the provider has
them; otherwise actual is `null` and the event is `due`, never `released`.

### Values, releases, and revisions

- CPI MoM uses seasonally adjusted indexes; CPI YoY uses unadjusted indexes.
  Payroll growth is the monthly employment-level difference in thousands of jobs.
  Calculations use exact periods and preserve their source inputs.
- Macro values are current-vintage observations, **not original release actuals**.
  Unknown publication timestamps remain null. NY Fed periods are effective dates.
- **Observed releases.** For each supported macro series, the latest period is
  exposed as a `released` calendar event derived from the stored observation,
  with real `actual`, preceding-period `previous`, and `revised_previous` when the
  source later changed that preceding value. No `scheduled_at` and no `consensus`
  are fabricated; BLS has no publication timestamp, so the shown release time is
  explicitly labeled *first observed by Omarkets*, not the official publication.
  Reference-rate series (EFFR, SOFR, target bounds) are excluded. Only the latest
  period per series is listed; full per-period history is not enumerated.
- Calendar schedules from BEA/Fed provide real scheduled times; BEA release RSS
  and Fed RSS provide real publication times. Schedule and notice records are not
  falsely joined.
- **Consensus/Forecast** is supplied by the consensus calendar provider when
  available (a market estimate, not official), and is otherwise `null` — never
  invented. Deterministic `actual - consensus` surprise is computed only when
  both exist. Percentage surprise is not computed for rates.
- SQLite preserves semantic changes as immutable local versions, including
  A→B→A. Unchanged retrievals do not create revisions. History means **observed
  since install**, not a reconstructed official first-release vintage. Rolling
  history windows do not overwrite valid calculations with incomplete inputs.
  A successful older-period response cannot make newer cached data live.

### Pages and freshness

Macro groups series by inflation, employment, growth, housing, and policy;
values show period, units, source, check time, and an expandable revision history.
Each series also renders a compact SVG **trend sparkline** from its recent
observed periods (bounded; no charting engine). Calendar defaults to Today, with This Week, importance/category/status filters,
actual/forecast/previous fields, and revision history. Events without a scheduled
time are grouped as observed releases (labeled first-observed) or published
notices, both distinct from scheduled times. Overview lists upcoming events with
New York/local times and client-side countdowns. Times use
`America/New_York` and browser-local timezone, including DST.

Persisted live economics lives in `economics-live.sqlite3` alongside DuckDB;
demo uses `economics-demo.sqlite3`. Namespace checks reject mixing. A failed
provider check retains data and marks affected series stale. Freshness measures
successful source checks, **not** whether a newer reference period should exist.
A successful older-period response cannot make newer cached data live. Calendar
freshness considers calendar sources, not unconfigured optional providers.
Internet loss is detected on the next check; the backend does not continuously
probe connectivity. Browser disconnection retains explicitly last-known data.

The consensus feed is rate-limited by its operator. The adapter treats HTTP 429
as an unavailable check, retains the last good calendar, and backs off on the
normal poll interval rather than retrying in a storm. Repeated manual probing can
trigger the limit; normal 1-hour polling does not.

Polling is owned by the service and persisted checks avoid immediate repeated
checks after restart. Requests have 15-second timeouts, a 20-second attempt
budget, 2 MB response limit, and at most two attempts with bounded retry delay.
`Retry-After` up to 30 seconds is honored; longer delays defer to a later polling
cycle. **Release-proximity polling** is implemented: when a calendar event for a
provider is scheduled within `OMARKETS_ECONOMIC_PROXIMITY_BEFORE_SECONDS`
(default 1800) or occurred within `..._AFTER_SECONDS`, that provider polls at
`OMARKETS_ECONOMIC_PROXIMITY_SECONDS` (default 300) and otherwise returns to its
normal interval. It only shortens, never lengthens. There is still no global
quota accounting across multiple Omarkets processes. Keep BLS checks
conservative and verify provider quotas before shortening intervals. Economic
REST polls every ~30 seconds and coalesced WebSocket invalidations trigger
earlier refreshes.

### Economics configuration and CLI

| Variable | Default | Meaning |
| --- | --- | --- |
| `OMARKETS_ECONOMIC_MODE` | `live` | `live`, `demo`, or `disabled`; never automatic fallback |
| `OMARKETS_ECONOMIC_DB_PATH` | `<data_dir>/economics-<mode>.sqlite3` | Optional separate SQLite path |
| `OMARKETS_FRED_API_KEY` | unset | FRED key; unset means those series are unavailable |
| `OMARKETS_BLS_API_KEY` | unset | Optional BLS registered key; keyless works, invalid key auto-falls back |
| `OMARKETS_CENSUS_API_KEY` | unset | Census data API key for `census_retail` (retail sales headline) |
| `OMARKETS_EIA_API_KEY` | unset | Reserved for Phase 3 energy market data |
| `OMARKETS_ECONOMIC_POLL_SECONDS` | `21600` | Fallback interval for providers without a named default |
| `OMARKETS_ECONOMIC_POLL_OVERRIDES` | `{}` | Named provider intervals in seconds, minimum 30 |
| `OMARKETS_ECONOMIC_TRANSITION_SECONDS` | `30` | Shared due-event check interval; no timer per event |

```bash
.venv/bin/omarketsctl macro
.venv/bin/omarketsctl macro CPI_YOY
.venv/bin/omarketsctl calendar
.venv/bin/omarketsctl calendar week
.venv/bin/omarketsctl next-event
.venv/bin/omarketsctl providers
OMARKETS_ECONOMIC_MODE=demo .venv/bin/omarkets
```

Demo runs only when explicitly selected and identifies its data as `demo`.
It simulates a scheduled CPI event, release, and revision; it is not a replacement
for missing public data. Stop the existing backend before starting another on
the same port. Phase 3 has not been started.

## Phase 3 market data

Market quotes come from real external feeds behind provider adapters. The default
`market_data_mode=live` composes them; `demo` always uses the deterministic
synthetic provider. A failed live feed is never silently replaced by demo data.

### Providers (verified live)

| Provider | Transport | Instruments | Freshness | Auth |
| --- | --- | --- | --- | --- |
| CNBC quote webservice | HTTPS polling (5s default) | NQ, ES, WTI (explicit front-month futures), US2Y, US10Y, DXY, VIX, VXN, WTI, GOLD, NVDA, MSFT, AAPL, AMZN, META, GOOGL, AVGO, TSLA | NQ/ES ~10 min **delayed**; yields/DXY ~seconds; index/equity closes after hours | none |
| Binance spot | HTTPS polling (10s default) | BTC | seconds | none |

Verified real fetch on 2026-09-18: NQZ26 29,918, ESZ26 7,734, US10Y 4.922%,
DXY 100.24, .VIX 15.44, .VXN 19.96, WTI, GOLD, BTC (WTI as `CLZ26` verified
2026-10-21). `NQ`/`ES` are the CME
quarters (H/M/U/Z); `VIX`/`VXN` are the CBOE **spot indices**, not futures;
`DXY` is the ICE U.S. Dollar Index, not an ETF or proxy.

### Futures contract selection

`NQ` and `ES` resolve to the active front contract with a simple, transparent
rule: CME quarterly expiry on the third Friday of H/M/U/Z, rolling
`OMARKETS_FUTURES_ROLL_DAYS` (default 8) calendar days before expiry. `WTI` is
the NYMEX `CL` contract (every month; last trade three business days before the
25th of the prior month, four if the 25th is not a business day) with the same
roll, requested from CNBC as e.g. `CLZ26`. CNBC's continuous `@CL.1` is not
used: on October 20, 2026 it kept returning the expired November contract's
14:30 ET last trade for about eight hours. `GOLD` still uses `@GC.1`. The
contract code, expiry, and provider symbol are exposed via the API, CLI, and UI.
There is no back-adjusted continuous series. Override with
`OMARKETS_FUTURES_ROLL_DAYS`; this is selection only, not a roll engine.

### LIVE vs DELAYED vs STALE

Feed quality is computed from the exchange timestamp versus receipt:
if the exchange timestamp lags receipt by more than `OMARKETS_MARKET_LIVE_LAG_SECONDS`
(default 60) during a session, the feed is `DELAYED`; a session-only date with no
intraday time is `STALE`; an out-of-session last value is `STALE` (the UI says
"Markets closed" for it, not stale); a live feed
older than its asset-class threshold is `STALE`; demo data is `DEMO`. Delayed
data is never displayed as live, and a missing value is `UNAVAILABLE`, never `0`.
Live-mode staleness thresholds (`OMARKETS_MARKET_STALE_OVERRIDES`) are realistic
(900s equity futures, 300s yields/commodities/FX, 120s crypto) because free
feeds are not tick-perfect.

### Running real mode

```bash
.venv/bin/omarkets                       # market_data_mode=live by default
OMARKETS_MARKET_DATA_MODE=demo .venv/bin/omarkets   # synthetic demo
.venv/bin/omarketsctl markets
.venv/bin/omarketsctl quote NQ
.venv/bin/omarketsctl contracts
.venv/bin/omarketsctl market-providers
```

`GET /api/v1/markets/status`, `/api/v1/market/providers`, and `/api/v1/contracts`
expose per-instrument feed status, source, provider symbol, contract, and
timestamps. `/api/v1/state` adds `feed_status`, `provider_symbol`, `contract_code`,
`contract_expiry`, and `latency_seconds` to each instrument.

### Phase 3 limitations

- **NQ/ES are delayed ~10 minutes on the free CNBC feed.** Delayed is labelled;
  it is not a live tick feed. Real-time or true tick NQ/ES needs a paid
  entitlement (e.g. a broker/data vendor).
- Intraday history for the NQ chart accumulates from polling; there is no vendor
  OHLC/tick history download. Chart remains a line of real observations.
- No websocket vendor feed; "stream" is bounded polling with backoff.
- No paid sources were purchased. EIA is configured but unused (energy is a
  later-scope item).

## Cross-market context

`GET /api/v1/cross-market` reports NQ's and ES's change, NQ-versus-ES relative
strength (aligned / outperforming / underperforming within a 0.10-point
tolerance) and the move in US2Y/US10Y (in basis points), DXY, VIX and VXN. It
states relationships only, never causes, and withholds a value when a leg is
stale. Technical analysis (VWAP, ATR, structure) was retired on October 10, 2026.

## Phase 5 news, economic-calendar and geopolitical ingestion

Phase 5 adds a deterministic, event-driven information layer that turns many
sources into one clean, standardized event stream:

```
SOURCE → FETCH → NORMALIZE → DEDUPLICATE → STORE → EVENT BUS
```

It implements exactly this and no more: it does not decide whether an event
matters. No LLM is called for any news event, and no sentiment or importance
score is invented. The desk passes recent headlines to its writer as context.

### Event model

Every fetched item becomes a typed `SourceItem` at the adapter boundary and a
canonical `MarketEvent` after normalization, regardless of origin. A
`MarketEvent` carries: `id`, `source`, `source_type`, `source_id`, `source_url`,
`headline`, `summary`, `published_at`, `received_at`, `language`, `event_type`,
`entities`, `instruments`, `countries`, `regions`, `raw_payload`, `metadata`,
`deduplication_key`, and `ingestion_status`. Central-bank metadata
(`central_bank`, `speaker`, `document_type`, `meeting_date`) and economic-calendar
detail (`event_code`, `period`, `actual`, `consensus`, `previous`,
`revised_previous`, `unit`, source `importance`, `status`) are carried as
structured sub-objects.

`event_type` is only ever a source-provided category or a deterministic keyword
mapping; unknown types are preserved as opaque strings. Source types are
`news`, `economic_calendar`, `central_bank`, `government`, `geopolitical`,
`commodity`, `market_data_event`, and `social`.

### Sources implemented

Only reachable public/official feeds are enabled by default. Each is a separate
adapter and fails independently.

| Source name | Type | Feed |
| --- | --- | --- |
| `fed_press_monetary` | central_bank | Federal Reserve monetary-policy RSS |
| `fed_press_all` | central_bank | Federal Reserve all-press RSS |
| `ecb_press` | central_bank | European Central Bank press RSS |
| `boe_news` | central_bank | Bank of England news RSS |
| `boj_whatsnew` | central_bank | Bank of Japan "What's New" RSS |
| `treasury_press` | government | US Treasury documents via the official Federal Register API feed |
| `ofac_actions` | geopolitical | US Treasury OFAC sanctions actions via the Federal Register API feed |
| `federal_register_presidential` | government | US Federal Register presidential documents |
| `state_press` | geopolitical | US State Department press RSS |
| `whitehouse_actions` | government | White House presidential actions RSS |
| `ec_press` | government | European Commission press RSS |
| `eia_today` | commodity | US EIA Today in Energy RSS |
| `energy_press` | commodity | US Department of Energy press RSS |
| `bbc_world` | news | BBC World News RSS |
| `guardian_world` | news | The Guardian World RSS |
| `npr_world` | news | NPR World RSS |
| `cnbc_top` | news | CNBC top-news RSS |
| `economic_calendar` | economic_calendar | Bridge over the Phase 2 calendar/observed releases |

Third-party news feeds are headline-level metadata only; no full article text is
stored beyond the source-provided summary. The economic-calendar bridge adapts
the Phase 2 calendar and observed releases into the event stream and never
recomputes a macro value.

### Normalization, entity extraction and deduplication

Normalization is shared by every source: whitespace is collapsed, timestamps are
converted to UTC (a missing or unparseable timestamp becomes `null`, never a
guess), and the raw payload is persisted verbatim. Deterministic entity and
instrument extraction runs before any classifier: fixed alias tables and
word-boundary patterns recognize countries, regions, institutions
(Fed/ECB/BOJ/OPEC/…), and canonical instruments (NQ, ES, DXY, WTI, GOLD, BTC,
tech tickers, …). This is cheap pre-processing, not AI.

Deduplication has two layers. First a deterministic key over the normalized
headline plus stable entity ids and source type. Second a conservative
near-duplicate check (Jaccard token overlap above `OMARKETS_NEWS_DEDUPE_THRESHOLD`,
within `OMARKETS_NEWS_DEDUPE_WINDOW_HOURS`, and only when instruments/countries
match). The canonical `MarketEvent` is deduplicated while each source report is
retained as a `SourceOccurrence`, so Omarkets knows "three independent feeds
reported this". Phase 6 may refine this with Jev novelty detection without
changing the storage contract.

### Storage

Events are persisted in SQLite (`<data_dir>/events.sqlite3` by default) in three
distinct forms: the verbatim `raw_events` payload, the canonical deduplicated
`events` row, and one `occurrences` row per source report. Instrument and country
link tables make the API filters indexed rather than JSON scans. Raw source
information is never destroyed after normalization.

### Scheduler, health and observability

Each source is polled independently at its own interval with bounded timeouts,
one retry with backoff on transient failures, and payload-size limits. One
broken source can never crash the pipeline; failures are recorded per source.
Each run logs what source ran, how many items arrived, normalized, duplicated,
stored and rejected, whether it failed, and how long it took.

Source health is exposed with `enabled`/`disabled`, last attempt, last success,
last error, recent event count, and a status of `healthy`, `degraded`, `offline`
or `disabled`.

## Omarchy integration

A thin Quickshell plugin (`omarchy/omarkets/`) makes Omarkets part of the Omarchy
desktop. The Omarkets core remains the source of truth; the shell renders.

- **Bar widget** — a compact launcher icon (Nerd Font `nf-md-finance`, U+F081F).
  The bar carries no market numbers; the full strip, event countdown and health
  live in the popup. When the backend is unreachable or the data is old, the bar
  shows the icon followed by `⚠` instead of appearing healthy.
- **Compact popup** — the latest desk note (headline and summary), the market
  strip (NQ, ES, US2Y, US10Y, DXY, VIX), the next economic event with ET/local
  times and a live countdown, a DATA-vs-AI health breakdown, and actions
  (`Open Omarkets`, `Calendar`, `Refresh`). Colours follow the Omarchy theme.
- **Notifications** — one desktop notification per desk note, sent by the core
  via `notify-send`.
- **Hotkeys** — see below.
- **Launcher** — the popup and `SUPER+M` open the workstation, **focusing an
  existing window** rather than spawning duplicates; `?section=` deep-links a page.

The plugin holds **no market logic**. It reads one file the collector writes,
`$XDG_STATE_HOME/omarkets/bar.json`, sourced from `GET /api/v1/shell/status`,
`/shell/context`, `/desk?limit=1` and `/theme`.

### Shell API

Two small, versioned, read-only endpoints present a compact projection of already
computed state, so QML never assembles internal models or does timezone math:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/v1/shell/status` | Bar payload (contract version 2): primary quote, strip, session, next event, `risk_tone` (`{tone, label, line}`, or null without market state), data/AI health, providers |
| `GET /api/v1/shell/context` | Popup payload: cross-market strip, upcoming events |

Both return safe defaults for unavailable sections (never a 500), so a missing
service cannot blank the shell. The popup shows the risk-tone line and the next
high-impact event with a countdown; QML only displays them (`risk_tone` is an
additive field, so the contract version stays 2).

### Hotkeys

| Binding | Action |
| --- | --- |
| `SUPER + M` | Open or focus Omarkets |
| `SUPER + CTRL + M` | Toggle the Omarkets popup |
| `SUPER + ALT + M` | Open Omarkets at the economic calendar |

The installer refuses to touch an existing binding outside its own marked block,
and backs up `bindings.lua` before appending or migrating its exact legacy popup
binding from `SUPER+SHIFT+M` to `SUPER+CTRL+M`. The stock Music shortcut is preserved. Existing Omarchy `SUPER+M` usage was checked; the block is self-contained and removable.

### Startup, services and logs

The backend and the desk are **one process**, so they run as one
`systemd --user` unit that also serves the built web app:

```bash
./omarchy/install-plugin --bindings     # install plugin, service, launcher, hotkeys
systemctl --user status omarkets
journalctl --user -u omarkets -f
systemctl --user restart omarkets
```

`omarchy/omarkets-start` delegates to the service when installed (so there is a
single owner and no duplicate backend), and falls back to a detached process.
`omarchy/omarkets-stop` stops both the service and any stray listeners.

### Install / uninstall

```bash
./omarchy/install-plugin                # idempotent; safe to re-run
./omarchy/install-plugin --no-service   # plugin only
./omarchy/uninstall-plugin              # remove plugin/launcher/service/hotkeys
```

The uninstaller never deletes databases or the repository. See
[`omarchy/omarkets/README.md`](../omarchy/omarkets/README.md) for the full detail.

## Qwen voice interface

Omarkets exposes a small, read-only **MCP** (Model Context Protocol) server so
the existing `qwen-omarchy-control` realtime voice assistant can answer market
questions from Omarkets itself. Qwen is **ears, conversational routing and
presentation**; Omarkets remains the market brain.

```
Qwen frontend → Omarkets MCP adapter → Omarkets localhost API
```

The adapter (`core/interfaces/mcp/`) is stdlib-only, never imports Omarkets
storage/analysis/service internals, never opens DuckDB, and never calls an LLM.
It hangs off the side over MCP tools; nothing is inserted into Qwen's realtime
audio loop.

### Voice tools

| Tool | Use for |
| --- | --- |
| `market_status` | "what's happening with NQ?", "how's the market?", "what are yields doing?" |
| `market_brief` | "give me the market brief", "catch me up" |
| `market_next_event` | "what's next?", "when is CPI?" |
| `market_calendar` | "what's on the calendar today?", "what's important this week?" |
| `market_macro` | "what's the macro picture?", "what was the latest CPI?" |
| `market_latest_events` | "any important news?", "what just happened?" |
| `market_latest_analysis` | "what's the latest analysis?", "what does Omarkets think?" (latest desk note) |
| `market_event_analysis` | "explain that CPI release" (the desk note for that event) |
| `market_latest_briefing` | "give me the morning brief", "what's the week ahead?" (latest pre-market / week-ahead note) |
| `market_usage` | "what is Omarkets costing me?" (this week's AI spend, Omarkets only) |
| `open_omarkets` | "open Omarkets", "show me today's calendar" |

`market_history_query`, `market_materiality_calibration`, `market_fomc_history`,
`market_similar_cases` and `market_calibration_review` remain listed so the Qwen
project's tool list still matches, but answer that the feature was retired.

Every tool returns a `spoken` string already phrased for speech plus a `stale`
flag, so the assistant cannot read delayed data as live. The tools are read-only:
there is no tool that edits configuration, thresholds, model policy, credentials
or databases, and no generic shell/file/URL tool.

### No unnecessary model invocation

Every tool only reads stored state. None writes a desk note or calls a model;
`market_brief` reuses the latest stored note and otherwise returns deterministic
state.

### Running the MCP server

```bash
python3 -m core.interfaces.mcp          # stdio MCP server (or: ./omarchy/omarkets-mcp)
omarkets-mcp                            # installed console entry point
```

It reads `OMARKETS_API_URL` (default `http://127.0.0.1:8765`); only loopback
http(s) URLs are accepted. The Qwen project registers it as the `omarkets`
server in its `frontend-mcp.json` and routes to it from its `PROMPT.md`. A tool
is only considered shipped when the MCP server definition, the frontend
allowlist and the routing prompt all agree — the Qwen project's
`tests/test_omarkets_surface.py`
(in the Qwen project) pins all three.

**Changes to routing require a fresh Qwen session**, not just a gateway restart:
a long-lived realtime session anchors on its own conversation history.

## API overview

The API is restricted to `127.0.0.1`. It has no authentication; do not expose
or reverse-proxy it publicly. Provider secrets are never returned in API data.

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/health` | GET | Application, storage, and provider health |
| `/api/v1/state` | GET | Current market state for the whole universe |
| `/api/v1/instruments` | GET | Configured instruments with current state |
| `/api/v1/instruments/{symbol}` | GET | Current state for one instrument (404 if unknown) |
| `/api/v1/history/{symbol}` | GET | Stored observations; `start`, `end`, `limit` params |
| `/api/v1/markets/status` | GET | Per-instrument value, feed status, source, contract, timestamps |
| `/api/v1/market/providers` | GET | Market-data routing (symbol → provider) and feed status |
| `/api/v1/contracts` | GET | Active NQ/ES front-month contracts |
| `/api/v1/cross-market` | GET | NQ vs ES, yields (bp), DXY, VIX/VXN |
| `/ws/market` | WS | Market-state snapshots; opt into economics with `?topics=macro`, news with `?topics=news`, or both |
| `/api/v1/macro/state` | GET | Latest series, per-series freshness, providers, upcoming events |
| `/api/v1/macro/series` | GET | Canonical series definitions |
| `/api/v1/macro/series/{id}` | GET | Bounded stored history; `all_versions`, `limit` |
| `/api/v1/calendar` | GET | Events filtered by `start`, `end`, `importance`, `category`, `status`, `limit` |
| `/api/v1/calendar/today` | GET | New York calendar day |
| `/api/v1/calendar/week` | GET | New York Monday through Sunday |
| `/api/v1/calendar/upcoming` | GET | Next `days` (default 30, maximum 365) |
| `/api/v1/calendar/{id}` | GET | Event and local revision versions |
| `/api/v1/providers` | GET | Economic provider states and check times |
| `/api/v1/events` | GET | Canonical events; filters `source`, `source_type`, `event_type`, `instrument`, `country`, `start`, `end`, `limit` |
| `/api/v1/events/latest` | GET | Most recent canonical events |
| `/api/v1/events/{id}` | GET | One event with its source occurrences |
| `/api/v1/sources` | GET | Configured news sources, types, event types and health |
| `/api/v1/sources/status` | GET | Compact source-health view |
| `/api/v1/sources/stats` | GET | Per-source ingestion counters from the last run |
| `/api/v1/shell/status` | GET | Compact, versioned bar payload for the native Omarchy widget |
| `/api/v1/shell/context` | GET | Compact, versioned popup payload (cross-market, events) |
| `/api/v1/desk` | GET | Today screen: quotes (with `closed` for a cash index outside its hours and an `explainer`), rates, upcoming events, notes, `next_event` (next high/critical release within 14 days, with `forecast_text` and `playbook_text`) |
| `/api/v1/bars/{symbol}` | GET | OHLC bars: `interval=5m\|15m\|1h`, `range=1d\|5d\|1m` (1m allows 15m and 1h only; 422 otherwise). See "Chart API" |
| `/api/v1/explainers` | GET | The explainer library: `{entries: [{key, kind, name, title, what, why_nq, rising, falling, rising_label, falling_label}]}`. See "Explainer library" |
| `/api/v1/session` | GET | The active (or last) Globex session with its segments (each with `explainer` text), the next daily break, the next open when closed, holidays and early closes in the next 21 days |
| `/api/v1/levels/{symbol}` | GET | Key levels for NQ or ES (404 otherwise), each with explainer text |
| `/api/v1/risk-tone` | GET | Rule-based risk tone `{tone, label, reasons[], inputs[], rules}` |
| `/api/v1/chart/pins` | GET | Release pins for `range=1d\|5d\|1m`, with result, NQ's 15-minute move and `note_id`; 503 without the desk |
| `/api/v1/sparklines` | GET | 15-minute closes over the 24 hours to each Today tile's latest quote; null marks a missing bucket |
| `/api/v1/desk/notes` | GET | Written notes by time: `from`, `to` (UTC offset required; half-open; default the 7 days before now), `limit` (1–500, default 100). Returns `{from, to, limit, truncated, notes: [{id, kind, at, headline, summary, tone}]}`, newest first; failed and draftless notes are left out |
| `/api/v1/desk/notes/{id}` | GET | One desk note |
| `/api/v1/desk/calendar` | GET | Calendar page: `week=this\|next` (New York Monday–Monday), `all` (default false: medium importance and up), `tz` (IANA zone the days are grouped in; default the desk's reader zone, 422 if unknown). Returns `{generated_at, week, start, end, tz, all, hidden, days: [{date, label, rows}], stale, coverage}`; see "Calendar page" below. 503 without the desk |
| `/api/v1/desk/ask` | POST | Write an "Explain the market now" note |
| `/api/v1/desk/playbook` | GET | How NQ reacted to each indicator's beats and misses; each release carries `note_id` (null when no note), `surprise`/`surprise_text` (actual minus forecast at the reporting precision), and each bucket `nq_15m_avg` (average first-15-minute NQ move, null when none measured) |
| `/api/v1/desk/macro` | GET | Macro page: `{generated_at, groups: [{key, title, cards}]}` in four groups (inflation, jobs, growth, fed); see "Macro page" below. 503 without the desk |
| `/api/v1/desk/news` | GET | News page: headlines from the last `hours` (1–72, default 24), newest first, one per title, tagged by fixed rules: `{generated_at, start, items, hidden, alerts}`; see "News page" below. 503 without the desk |
| `/api/v1/desk/learn` | GET | Every note's Learn box, newest first, repeats merged: `{count, taught, entries, topics, most_taught}`; each entry has `topic`/`topic_label`/`heading` from fixed keyword rules (`core/desk/lessons.py`) |
| `/api/v1/ai/budget` | GET | AI cost guard status (weekly limit, spend, reservations) |
| `/api/v1/theme` | GET | Active Omarchy palette (`/theme/stream` SSE, `POST /theme/reload`) |

Calendar events (`/calendar`, `/calendar/today|week|upcoming`) carry `note_id`:
the desk note written for that release, or null. The core matches it from the
note id `release:<YYYYMMDDTHHMM>:<CODE>[+<CODE>…]` (the release minute in UTC and
the event codes out at that moment): same UTC minute, event code in the list,
US events only, written notes only (`core/desk/links.py`). No text matching.

### Chart API

All computed in the core (`core/analysis/bars.py`, `levels.py`, `risk_tone.py`,
`core/desk/pins.py`, sessions in `core/domain/sessions.py`); the web app only
draws. Times are UTC; unavailable values are `null` with a reason, never zero.

- **Bars** (`/api/v1/bars/{symbol}`): a range is whole Globex sessions from the
  session engine: `1d` the active (or last completed) session, `5d` the last
  five trading sessions, `1m` the trading sessions of the last 30 days. Only
  quotes inside a session are read (one query, every session's exact bounds), so
  the maintenance break, weekends, holidays and the time after an early close
  never enter a bar. Bars are aligned to the interval in UTC (ET is a whole
  number of hours from UTC). Open/close are by exchange time; a re-sent exchange
  time keeps its latest receipt. A quote is used only if it arrived at most an
  hour after its exchange time and not more than a minute before it: CNBC re-sends
  the settle at each Globex open stamped 01:00 ET (~17 hours early) and anchors
  date-only quotes to 16:00 ET, and neither is a trade at that time. Between a
  session's first and last quote a missing bar is returned as a gap (`count: 0`,
  prices null); nothing is added before the first or after the latest quote.
  Each bar has `source`, `contract`, `delayed` (from its first and last quote)
  and `change_pct` (close vs the range's first open, used for overlays). The
  response has `sessions` (with segments), `last_at`, `delayed`, the quote's
  current `feed_status`/`status`, `sources`, `contracts`, `fine_since` and a
  `note` when the range reaches back past the 30-day full-detail retention
  (older data is one quote a minute, so a brief spike can be missing).
- **Session segments** (`session_segments`): Asia from the 18:00 ET Globex open
  to the London open (08:00 Europe/London, so UK clock changes are respected);
  Europe to 08:00 ET; pre-market 08:00 ET to the 09:30 cash open; US cash; after
  the cash close to the Globex close (13:15 ET on early-close days). VIX and VXN
  are computed 03:15–16:15 ET on trading days (`volatility_index_open`).
- **Key levels** (`/api/v1/levels/{symbol}`): overnight high/low (Globex open to
  09:30 ET, `partial` before then), prior settle (the source's previous close),
  prior day high/low (the previous full 18:00–17:00 ET session), prior week
  high/low (the previous Monday–Friday sessions) and gap vs settle (before 09:30
  ET: latest price minus settle; after: first cash price minus settle). Quotes
  from another futures contract (after a roll) make a level unavailable.
- **Risk tone** (`/api/v1/risk-tone`): NQ ±0.25%, 2-year yield ±3bp, dollar
  ±0.25%, VIX ±5% or at 25+, each since the previous close. Rising NQ, falling
  yields, a falling dollar and a falling VIX count for risk; the opposite
  against. Two or more one way and none the other: risk-on/off; one: leaning;
  both ways: mixed; none: quiet; fewer than two usable inputs or no NQ: unclear.
  Stale inputs are left out, except VIX after its close (its closing change).
- **Pins** (`/api/v1/chart/pins`): release moments of medium importance or more
  (Fed speeches left out) in the range, `high` or `medium`, with the headline
  result, NQ's move in the first 15 minutes once the delayed feed has it, the
  rule-of-thumb explainer and the desk note's `note_id`.

### Calendar page

`GET /api/v1/desk/calendar` (`core/desk/calendar.py`, no AI). Each row is one
release moment (same-time calendar rows merged, as for notes and pins):
`at`, `title`, `codes`, `importance`, `kind` (`economic`/`fed`/`speech`),
`released`, `venue`, `figures[]` (the desk `Figure`: actual/forecast/previous
values and texts, `verdict`, `surprise` sized against the figure's earlier
surprises, plus `reading` `better`/`worse`/null and `reading_text`, read for the
economy from the figure's reading family; null when unscored or for Fed
events), `nq_15m`/`nq_15m_text` with `nq_15m_status`
(`upcoming`/`waiting`/`measured`/`unavailable`), `note_id`, `explainer_key`
(e.g. `indicator:CPI`) and `explainer`, and `playbook_text`/`playbook` (the
playbook entry, at most 12 releases; null for Fed events). Unavailable values
are null, never zero. `/calendar*` payloads are unchanged.

### Macro page

`GET /api/v1/desk/macro` (`core/desk/macro.py`, no AI). Fourteen cards: CPI,
core CPI, PCE and PPI year over year; unemployment, payrolls, average hourly
earnings and job openings; GDP, retail sales and ISM manufacturing; the Fed
funds target, the next Fed decision and the 2-year yield. Each card has
`value`/`value_text` (null and "—" when unavailable, never zero),
`period_text`, `trend` (`rising`/`falling`/`steady`/`unknown`: the mean of the
last 3 readings, 2 for GDP, against the 3 before, with a per-card "steady"
band; the 2-year uses its week change, ±5bp), `trend_text`, `nq_when_rising`
(`bad`/`good`/`mixed`) with `nq_text`, `nq_effect` (the trend's usual meaning
for NQ, used for colours: jobs and growth are `mixed` and stay plain),
`points` (recent readings for the sparkline), `release` (the newest calendar
release of the card's figure that had a forecast, as a Calendar figure with its
better/worse read for the economy, `text` and `nq_effect`), `explainer_key`,
`sources` (provider, series, adjustment, release time, revision, merged
duplicates; shown behind ⓘ), `stale`, `at` (next Fed decision) and
`quote_symbol` (the 2-year card takes its sparkline from `/sparklines`). PPI
from FRED and BLS is merged into one card.

### News page

`GET /api/v1/desk/news` (`core/desk/headlines.py`, no AI). Each item:
`id`, `at`, `source`/`source_label`, `headline`, `url`, `category`
(`fed`/`economy`/`big_tech`/`energy`/`geopolitics`, from fixed keyword and
source rules; null means not market-moving and counts toward `hidden`),
`category_label`, `scheduled` (published from 5 minutes before to 90 minutes
after a medium-or-higher release or Fed event of its kind), `nq_15m`/
`nq_15m_text` with `nq_15m_status` (`unmeasured` for uncategorised headlines,
`waiting`, `measured`, `unavailable`; the same measurement as the chart pins,
NQ read once per request) and `alert` (it also fired a breaking alert).
`alerts` are the breaking alerts in the window.

### Explainer library

Every fixed hover text lives in `core/desk/explainers.py` (no AI, written once;
rules of thumb, never advice). An entry's `key` is `<kind>:<name>`, where `kind`
is `instrument` (strip symbols, e.g. `instrument:NQ`), `indicator` (calendar
event codes, e.g. `indicator:CPI`), `level` (key-level keys), `segment` (session
segment names), `reading` (what-to-watch families: inflation, jobless, growth,
fed) or `topic` (`rates`, `treasury_auction`). `rising`/`falling` say what a
rise (for indicators and readings: a figure above/below forecast; for levels:
price above/below it) usually means, labelled by `rising_label`/`falling_label`;
both are null where there is nothing useful to say (segments, some Fed events).

`GET /api/v1/explainers` serves the whole library (static while the core runs;
the web app fetches it once). The older single-string fields are kept and now
come from the library: `Quote.explainer` and `RatesView.explainer` (what + why),
`UpcomingItem.explainer`, `WatchCard.explainer` and pin `explainer` (the
indicator's what + why), `WatchCard.readings` (the reading entries),
`KeyLevel.explainer` (everything in one paragraph) and the new session
`segments[].explainer`. Hover cards for quote tiles and key levels show the full
entry once the library has loaded, and the short field before then.
`docs/explainers-review.md` lists every entry for review; regenerate it with
`.venv/bin/python -m core.desk.explainers > docs/explainers-review.md` (a test
fails when it is out of date).

Calendar ranges are half-open (`start <= time < end`), with aware timestamps;
limits are 1–500. Release notices without a scheduled timestamp use publication
time for filtering. History is bounded and is not a full official vintage archive.

Opted-in WebSocket clients receive typed `economic_event_upcoming`,
`economic_event_due`, `economic_event_released`, `economic_event_revised`,
`macro_series_updated`, and `provider_status_changed` messages with
`data: {ids, occurred_at}`. These are coalesced refresh notifications, not a
lossless release log. `ids: ["*"]` requests full REST refresh after overflow.
The frontend retains one socket; reconnect reloads REST state. Default clients
continue to receive only market snapshots.

News clients (`?topics=news`) receive `market_event_created`,
`market_event_updated` and `source_status_changed`, coalesced the same way. Event
creation/update payloads contain changed ids and the occurrence time, not full
event snapshots; REST is the recovery source.

Interactive docs are available at `http://127.0.0.1:8765/docs` while running.

A WebSocket message has the envelope `{"type": "state", "data": {...}}`; the
`data` object is the full market state. A client receives the current snapshot
immediately on connect and then latest-wins snapshots as updates arrive. The service runs
normally with no clients connected. Clients must not send application frames —
any inbound client frame ends the subscription (this is what the frontend
relies on; it sends no heartbeats).

### Market state

Every instrument in the universe is always present. Missing data is reported as
`unavailable`, never omitted and never shown as zero.

```json
{
  "generated_at": "2026-09-17T05:56:16Z",
  "provider": "fake",
  "provider_status": "ok",
  "instruments": {
    "NQ": {
      "value": 24780.9,
      "previous_close": 24812.5,
      "change": -31.6,
      "change_pct": -0.13,
      "unit": "index points",
      "timestamp": "2026-09-17T05:56:15Z",
      "age_seconds": 0.7,
      "stale_after_seconds": 5.0,
      "status": "live",
      "source": "fake"
    }
  },
  "live_count": 18,
  "stale_count": 0,
  "unavailable_count": 0
}
```

Status is one of `live`, `stale`, or `unavailable`. `change`/`change_pct` are
`null` when no baseline is known — they are never fabricated as zero.

## Configuration

Configuration comes from defaults, an optional `.env` file, and environment
variables prefixed with `OMARKETS_`. See [`.env.example`](.env.example) for the
full list. Commonly used:

| Variable | Default | Meaning |
| --- | --- | --- |
| `OMARKETS_API_HOST` | `127.0.0.1` | API bind address |
| `OMARKETS_API_PORT` | `8765` | API port |
| `OMARKETS_LOG_LEVEL` | `INFO` | Log level |
| `OMARKETS_DATA_DIR` | `./data` | Data directory |
| `OMARKETS_DUCKDB_PATH` | `<data_dir>/omarkets-<mode>.duckdb` | Market observation database; namespaced per mode so demo never mixes with live |
| `OMARKETS_DATA_PROVIDER` | `fake` | Provider adapter (`fake` only in Phase 1) |
| `OMARKETS_PROVIDER_UPDATE_INTERVAL` | `2.0` | Seconds between synthetic updates |
| `OMARKETS_PROVIDER_SEED` | `7` | Deterministic RNG seed for the fake provider |
| `OMARKETS_STALE_DEFAULT_SECONDS` | `30` | Default freshness threshold |
| `OMARKETS_STALE_OVERRIDES` | `{}` | Per-asset-class thresholds, as JSON |

See [Phase 2 economics](#phase-2-economics) for economic variables and
[Phase 5 ingestion](#phase-5-news-economic-calendar-and-geopolitical-ingestion)
for news variables. News variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `OMARKETS_NEWS_ENABLED` | `true` | Enable the ingestion pipeline |
| `OMARKETS_NEWS_ENABLED_SOURCES` | `[]` | JSON allow-list; empty = all enabled catalog sources |
| `OMARKETS_NEWS_DISABLED_SOURCES` | `[]` | JSON deny-list of source names |
| `OMARKETS_NEWS_POLL_SECONDS` | `300` | Default per-source poll interval; a slower catalog interval wins |
| `OMARKETS_NEWS_POLL_OVERRIDES` | `{}` | Per-source intervals, JSON, minimum 30s |
| `OMARKETS_NEWS_DEDUPE_THRESHOLD` | `0.6` | Near-duplicate Jaccard threshold (0–1) |
| `OMARKETS_NEWS_DEDUPE_WINDOW_HOURS` | `36` | Maximum merge window |
| `OMARKETS_NEWS_DB_PATH` | `<data_dir>/events.sqlite3` | Event store path |

Desk variables are listed in [the desk design](desk.md#configuration).
`OPENROUTER_API_KEY` (or `OMARKETS_OPENROUTER_API_KEY`) is the desk writer's
credential; without it the desk shows its data but writes no notes.

## Instrument universe

Canonical symbols (not vendor tickers — provider adapters map onto these):

**Primary:** NQ

**Context:** ES, US2Y, US10Y, DXY, VIX, VXN, WTI, GOLD, BTC

**Technology:** NVDA, MSFT, AAPL, AMZN, META, GOOGL, AVGO, TSLA

## Architecture (high level)

```
External data → deterministic ingestion → normalization/validation → storage
→ deterministic facts (core/desk/facts.py) → one AI writer call per note
→ Today screen, Omarchy popup + notifications
```

Core principle: **LLMs interpret structured information.** They do not perform
deterministic calculations, raw data collection, or persistent state management.

Phase 1 implements the deterministic path end to end:

```
FakeMarketDataProvider → MarketStateService → DuckDB storage
                                         ↘ FastAPI + WebSocket
```

| Package | Role |
| --- | --- |
| `core/domain` | Typed schemas and the canonical instrument universe |
| `core/providers` | Provider abstraction and the deterministic fake provider |
| `core/storage` | Repository abstraction and the DuckDB implementation |
| `core/services` | Current market state, staleness, subscriber fan-out |
| `core/api` | FastAPI app, REST routes, WebSocket endpoint |
| `core/analysis` | Deterministic economic and cross-market calculations |
| `core/desk` | Desk facts, writer prompt, scheduler and note store |
| `core/theme` | Omarchy palette reader |
| `core/notifications` | Desktop delivery boundary (`notify-send`) |
| `core/config` | Typed settings and logging |
| `core/main.py` | Application entrypoint (`omarkets`) |
| `core/cli.py` | Operator CLI (`omarketsctl`) |
| `web/` | React/TypeScript/Vite app (Today + Details) — thin client over the API |
| `omarchy/` | Launcher scripts plus the Quickshell bar widget/popup plugin |

`core/ingest` holds news normalization, deterministic entity extraction and
deduplication.

Full detail: [`docs/architecture.md`](architecture.md).

## What Phase 1 + 1.5 do

- Typed, validated schemas for instruments, observations, state, and quotes
- A deterministic, offline synthetic provider (`seed` + injected clock)
- Durable DuckDB storage with provenance and UTC timestamps
- Current market state with absolute/percent change and explicit freshness
- Configurable per-asset-class staleness (`live` / `stale` / `unavailable`)
- A localhost FastAPI REST API and a WebSocket state stream
- Clean async startup/shutdown with no orphaned tasks
- Structured logging and a `omarketsctl` status/state CLI
- Tests covering schemas, the provider, storage, the state service, the API,
  the WebSocket, staleness, and regressions
- A dark, dense React workstation (since replaced by the seven-page UI)
- A single managed WebSocket connection with capped-backoff reconnect,
  ~3 s REST state/health polling fallback, and explicit
  `live / last-known / stale / unavailable` presentation
- A Phase 5 News/Events page: chronological feed with filters, deterministic
  instrument/country/entity tags, an upcoming/released economic-calendar table,
  and per-source ingestion health
- Phase 2 economics: real BLS/NY Fed observations, BEA/Fed calendars, revision
  history, provider health, and Macro/Calendar pages. See
  [Phase 2 economics](#phase-2-economics) for the honest coverage list — some
  requested families are not operational yet.
- Omarchy launch scripts and an optional desktop entry (no native shell yet)

## Not implemented

- **Real-time market quotes.** Real feeds are live, but NQ/ES are delayed on the
  free source; there is no paid real-time entitlement configured.
- **Hermes.** Hermes is an optional general-purpose coding/agent tool the voice
  assistant may launch; it is not part of the desk runtime or the voice
  pipeline and Omarkets works without it.
- **Trading.** No signals, recommendations, or order execution. The desk writer
  is told not to give trading advice and its output is rejected if it does.
- **News full text / paywalled providers.** Only public/official feeds and
  headline-level metadata are ingested; no article scraping.
- **Native Omarchy shell (beyond the bar widget).** The plugin adds a bar label,
  a compact popup, an event countdown and hotkeys. There is no full QML
  replacement for the web app.
- **Authentication.** None; the service is loopback-only.
- **Market sessions/holidays.** The desk uses rule-based US holidays and New
  York session times; a known delayed feed is labelled `DELAYED` and a shut
  market's last value is shown as "Markets closed", not as a stale quote.
- **Infrastructure.** No Redis, Kafka, Kubernetes, microservices, or Docker.
- **Forecasting.** Not implemented.

## Development

**Running** Omarkets is the Omarkets core: deterministic engines plus the desk
writer. An optional voice assistant can read it over the read-only MCP adapter.
Neither the voice assistant nor any general-purpose agent is a dependency of
analysis.

Run the checks before sending a change:

```bash
.venv/bin/pytest -q
(cd web && npm test && npm run typecheck && npm run build)
```
