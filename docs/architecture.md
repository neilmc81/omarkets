# Omarkets Architecture

## October 20, 2026: the desk replaces the analyst chain

The Phase 6 (Jev decisions/escalation), Phase 7 (analyst service), Phase 10
(briefings/workflows), Phase 11/12 (historical outcomes, calibration review,
similarity) and retired technical-analysis code described in the sections below
was removed. The live path is now: deterministic ingestion and storage →
deterministic desk facts (`core/desk/facts.py`) → one OpenRouter writer call per
note (up to three calls: primary, fallback, one regeneration) through the AI cost guard → Today screen, Omarchy popup and notifications.
See [the desk design](desk.md). The sections below are kept as history: they
describe removed code (for example the analyst chain, Jev and the
`*_ANALYST_*` settings in §6) and are **not** the current design. For values in
force, run `omarketsctl config`; code defaults and old docs can differ from it.

## October 10 scope revision: technical analysis retired

The current repair candidate removes the active TechnicalService, technical
analyst dispatch, technical REST/UI/MCP surfaces, indicator/structure workflow
triggers and technical briefing requirements. Raw quote history and nontechnical
cross-asset context remain: `/api/v1/cross-market` derives its existing typed
response directly from market state. Legacy technical configuration and persisted
schemas remain readable, but do not enable new technical work. No historical
records or accounting are deleted. Web/QML removal and deployment acceptance are
tracked in [the active plan](plans/archive/marketos-active-repair-plan.md) and
[ledger](plans/archive/marketos-repair-ledger.md).

The phase descriptions below are historical architecture documentation where
they describe technical calculations; they do not override the revised scope.

Status: **Phase 1/1.5 complete; Phase 2 operational; Phase 3 real market data;
Phase 4 deterministic technical & cross-market state; Phase 5 deterministic news,
economic-calendar and geopolitical ingestion; Phase 6 TypeSafe Jev materiality &
decision routing; Phase 7 native analyst layer (PydanticAI + Ollama Cloud); Phase 8
native Omarchy integration; Phase 9 Qwen voice interface; Phase 10 daily
briefings & event-driven workflow automation; Phase 11 historical intelligence
& calibration; Phase 12 calibration review + historical similarity intelligence.**
The synthetic market path remains intact and is used only in explicit demo mode.
Real feeds now supply NQ/ES (explicit front-month futures), Treasury yields, DXY,
VIX/VXN, commodities, equities and BTC, each with an explicit feed status.
NQ/ES are ~10 minutes delayed on the free CNBC source and are labelled `DELAYED`.
Macro/economics (28 series) and the BEA/Fed/consensus calendars remain
operational. Phase 4 derives session levels, VWAP (only with valid volume),
confirmed swing structure, structure breaks, ATR, opening range, NQ-vs-ES
relative strength and cross-market context deterministically. Phase 5 ingests
central-bank, government, geopolitical, commodity, news and economic-calendar
items from public/official feeds into one typed, deduplicated event stream with
source health. Phase 6 adds TypeSafe Jev (through the direct TypeSafe API) as a structured
decision engine: it classifies, scores and routes each event, the deterministic
materiality engine combines Jev beliefs with measured market facts, and escalated
events are queued for Phase 7. Phase 7 consumes that escalation queue in a native
Omarkets analyst service: a bounded async worker runs the analyst targets named by
the Phase 6 router through PydanticAI + Ollama Cloud reasoning models, validates a
typed `AnalystResult`, runs optional synthesis, and persists/streams the result.
Jev never generates analysis; Phase 7 analysts interpret only deterministic facts
already computed by the core. Phase 8 adds native Omarchy integration: a thin
Quickshell bar widget and compact market popup, deterministic native notifications
for high-materiality events, and a popup hotkey. Phase 9 exposes Omarkets to the
existing Qwen realtime voice assistant through a small, read-only MCP tool surface
(`core/interfaces/mcp`) that talks only to the loopback HTTP API: Qwen is ears,
routing and presentation, while Omarkets remains the source of truth and no
analysis runs inside the voice loop. **Hermes is not part of the analyst runtime or
the voice pipeline** — it is an optional general-purpose agent Qwen may launch, and
Omarkets works whether Hermes is installed or not. No trade signal or order
execution is implemented.

---

## 1. Purpose

Omarkets is a local, deterministic market-analysis workstation built on an
Omarchy Linux desktop. It ingests market and macro information, computes
analytics deterministically, maintains a structured market state, and presents
that state to LLM analysts for interpretation. It surfaces results through a
thin Omarchy UI and notifications.

Omarkets is **not** a trading bot. It does not place orders and does not
automate execution.

Primary market: **Nasdaq futures (NQ)**.

Context set (initial): ES, US 2Y yield, US 10Y yield, DXY, VIX/VXN, major
Nasdaq technology names, semiconductors, crude oil, gold. The context set may
grow later.

---

## 2. Data and control flow

```
External data
   ↓
Deterministic ingestion
   ↓
Normalization / validation
   ↓
Storage
   ↓
Deterministic analytics
   ↓
Market state
   ↓
AI interpretation
   ↓
Materiality / synthesis
   ↓
Omarchy UI + notifications
```

Each stage has a single owner and a clear responsibility.

| Stage | Owner | Responsibility |
| --- | --- | --- |
| External data | providers | Vendor APIs, downloads, feeds. Untrusted input. |
| Deterministic ingestion | `core/providers` (Phase 1) / `core/ingest` (pipeline) | Adapters behind interfaces. No vendor types past the boundary. |
| Normalization / validation | `core/domain`, `core/providers` | Schemas, units, timezone normalization, rejection of malformed data. |
| Storage | `core/storage` | DuckDB (analytical/time series), SQLite (small relational state). The market observation database is namespaced per data mode (`omarkets-<mode>.duckdb`) so synthetic demo observations can never win a latest-wins comparison against real live quotes; a one-time, non-destructive migration copies real rows from the pre-namespacing database. |
| Deterministic analytics | `core/analysis` | Indicators, cross-asset relationships, materiality scoring. |
| Market state | `core/services` (Phase 1) / `core/analysis` (future) | A single structured snapshot of "what is true now". |
| AI interpretation | `core/desk/writer.py` (one OpenRouter call per note (up to three: primary, one fallback, one regeneration if it invents numbers), via the AI cost guard) | Explain deterministic facts in plain English. Never compute basics or fetch raw data. |
| Omarchy UI + notifications | `omarchy/`, `core/notifications` | Display and alert. No market logic. |
| Local API / WebSocket | `core/api` | FastAPI. The only interface the UI uses. |

Phase 7's end-to-end path (Hermes is deliberately absent):

```
MARKET / MACRO / NEWS DATA
        ↓
Deterministic Omarkets engines
        ↓
TypeSafe Jev (TypeSafe API)        core/providers/decisions
        ↓
Materiality + Routing              core/analysis/materiality.py, routing.py
        ↓
Escalation Queue                   core/storage/decisions.py
        ↓
Omarkets Analyst Service           core/services/analysts.py  (queue owner)
        ↓
PydanticAI                         core/ai/providers/ollama.py
        ↓
Specialist Analysts                core/ai/analysts/*.py
        ↓
Ollama Cloud (DeepSeek / GLM)
        ↓
Typed Analyst Results              core/domain/analysis.py
        ↓
Optional Synthesis
        ↓
Omarkets Database → API / WebSocket → Frontend
```

Hermes sits outside this pipeline as a future human-facing client:

```
USER → Hermes → Omarkets API → existing Omarkets system
```

Omarkets analysis works whether Hermes is running, crashed, changed, or removed.

---

## 3. Layering rules

### 3.1 Deterministic before AI

LLMs interpret structured information. They must not:

- perform basic arithmetic or indicator calculation,
- collect raw data from providers,
- own persistent state,
- be the source of a market fact.

Anything a conventional program can compute reliably is computed first and
handed to the LLM as structured context, together with its evidence.

### 3.2 Deterministic vs non-deterministic

- **Deterministic:** ingestion, normalization, storage, analytics, state,
  materiality scoring inputs, API payloads. Reproducible and testable.
- **Non-deterministic:** analyst interpretation and synthesis. Allowed to be
  probabilistic, but must cite the structured evidence it was given.

### 3.3 Thin UI

`omarchy/` (Quickshell/QML) and `web/` (charting) hold presentation only. They:

- read from `core/api` over localhost HTTP/WebSocket,
- never open a database,
- never call a market-data provider,
- never orchestrate LLMs.

The Python core is the single source of truth for market facts.

### 3.4 Provider isolation

Vendor-specific code lives in adapters under `core/providers` (implementing the
`MarketDataProvider` interface from `core/providers/base.py`). Adapters translate
vendor payloads into internal typed schemas. No provider SDK types appear in
domain models, storage, analysis, or API layers.

> Phase 1 note: the bootstrap layout reserved `core/ingest` for adapters. The
> Phase 1 specification placed the provider abstraction and fake adapter under
> `core/providers` instead, and that is where the interface now lives. As real
> adapters arrive, `core/ingest` is the natural home for normalization and
> ingestion *pipeline* logic that sits above a provider adapter; either way,
> vendor code stays behind the `MarketDataProvider` interface.

### 3.5 Typed boundaries

Cross-service and cross-layer boundaries use explicit validated schemas
(Pydantic). Loose dicts are not an interface.

---

## 4. Component map

```
core/
├── api/            FastAPI app factory, REST routes, WebSocket endpoint
├── config/         typed settings (pydantic-settings) and logging
├── desk/           desk facts, writer prompt, scheduler and note store
├── domain/         instruments (canonical universe), typed schemas
├── providers/      MarketDataProvider, NewsSource, economic + earnings adapters
├── services/       MarketStateService, NewsService, EconomicService,
│                   AICostGuard, fan-out
├── storage/        DuckDB observations, SQLite event and economic stores
├── ingest/         bars, Phase 5 normalization, entity extraction, dedupe
├── analysis/       deterministic indicators, economics, materiality, routing,
│                   plus the Phase 8 notification policy
├── workflows/      Phase 10 scheduler, briefing builders, event profiles,
│                   calendar reconciliation
├── historical/     Phase 11 outcome measurement, snapshots, cohorts,
│                   calibration, anchor policies
├── notifications/  notify-send delivery boundary (Phase 8)
├── main.py         application entrypoint (`omarkets`)
└── cli.py          operator CLI (`omarketsctl`)

omarchy/            launchers + Quickshell/QML: Phase 8 bar widget + market popup
web/                local charting frontend (e.g. Lightweight Charts)
tests/              mirrors the source tree
docs/               architecture and design notes
```

### 4.1 Implemented in Phase 1

| Component | Location | What exists |
| --- | --- | --- |
| Instrument universe | `core/domain/instruments.py` | 18 canonical symbols across 7 asset classes; immutable registry |
| Typed schemas | `core/domain/models.py` | `MarketObservation`, `PriceQuote`, `InstrumentState`, `MarketState` |
| Provider interface | `core/providers/base.py` | `MarketDataProvider` with `start`/`fetch_snapshot`/`stream_updates`/`health`/`aclose` |
| Fake provider | `core/providers/fake.py` | Deterministic seeded random walk / mean-reverting simulation, injectable clock |
| Storage | `core/storage/duckdb_repo.py` | `DuckDBObservationRepository`: init, store, latest-per-instrument, history, health |
| Market state | `core/services/market_state.py` | `MarketStateService`, pure `build_market_state`, `StalePolicy`, latest-wins fan-out |
| API | `core/api/` | FastAPI factory + lifespan, REST routes, `/ws/market` |
| Config/logging | `core/config/` | `Settings` (env prefix `OMARKETS_`), `configure_logging` |
| Entrypoints | `core/main.py`, `core/cli.py` | `omarkets`, `omarketsctl status|state` |

Data flow within Phase 1:

```
FakeMarketDataProvider
        │  stream_updates()
        ▼
MarketStateService ──► DuckDB storage (observations)
        │                    │
        │ current_state()    │ latest_per_instrument() seeds state on startup
        ▼                    ▼
FastAPI REST + WebSocket /ws/market
```

**Threading model.** Uvicorn runs with `workers=1`. One lock guards each DuckDB
repository connection; market writes and historical observation reads use worker
threads rather than holding the API loop. Health checks use the last verified
storage result if that lock is busy, then probe again when it is free. REST
handlers consume the loop-owned cached market state.

Historical source callbacks project their inputs on the owning thread. Pure
outcome measurement and atomic historical writes then run sequentially on the
single history worker; shutdown waits for any in-flight work. Measurements for
the same cross-asset window are reused within one anchor without changing
outcome identities, evidence or method versions. Analyst input construction
awaits the decision service's asynchronous history reader, while SQLite reads
and state projections remain on their owning thread.

**Restart semantics.** The in-memory state cache is seeded from storage at
startup via `latest_per_instrument()`, so a restart reports the last known
values (marked `stale` once they exceed their threshold) rather than pretending
there is no data.

---

### 4.1a Web UI today (redesign, October 2026)

The Phase 1.5 workstation described below (`App.tsx`, `useMarket`, `format.ts`,
the economics and news workstation pages) was removed in the UI redesign
. The web app is now one shell with seven pages,
each reading a core view model and doing no market calculation:

| Page | Location | Core endpoint(s) |
| --- | --- | --- |
| Shell, `<Explain>`, status panel | `web/src/shell/` | `/health`, `/api/v1/desk` (feeds dot and the status panel behind it) |
| Today | `web/src/today/` | `/api/v1/desk`, `/session`, `/levels/NQ`, `/risk-tone`, `/sparklines`, `/chart/pins` |
| Chart | `web/src/chart/` | `/bars/{symbol}`, `/levels`, `/chart/pins` |
| Calendar | `web/src/calendar/` | `/api/v1/desk/calendar` |
| Macro | `web/src/macro/` | `/api/v1/desk/macro` |
| News | `web/src/newsboard/` | `/api/v1/desk/news` |
| Playbook | `web/src/playbook/` | `/api/v1/desk/playbook` |
| Learn | `web/src/learn/` | `/api/v1/desk/learn` |

Hover text comes from the explainer library (`/api/v1/explainers`), desk notes
and playbook counts; no AI runs on hover. The pages poll; the browser no longer
opens the market-state WebSocket (the core still serves it for other clients).
The older `/api/v1/macro/*`, `/calendar*`, `/events*` and `/state` endpoints
remain for the CLI, the Omarchy layer and tests.

### 4.2 Implemented in Phase 1.5 (web parts superseded by 4.1a)

| Component | Location | What exists |
| --- | --- | --- |
| Workstation | `web/src/App.tsx` | Overview, clocks, strip, context, health, five future-module empty sections |
| Validated contracts | `web/src/api/` | Zod schemas with inferred TypeScript types; centralized cancellable REST requests |
| Transport state | `web/src/hooks/useMarket.ts` | One reconnecting WebSocket, generation ordering, REST freshness/health refresh, last-known state |
| Observation chart | `web/src/chart/` | Lightweight Charts NQ line; sorted bounded history merge, revision handling, resize and cleanup |
| Formatting | `web/src/format.ts` | Null-safe values, yield/basis-point display, timezone-aware clocks, signed change styles |
| Desktop launch | `omarchy/` | Webapp launcher and optional non-overwriting desktop-entry installer |
| Frontend tests | `tests/web/` | Vitest/jsdom contracts, lifecycle, rendering, formatting and chart regressions |

React/TypeScript/Vite runs as a separate loopback process. Dev (`5173`) and
built-file preview (`4173`) proxy `/api`, `/health` and `/ws` to the FastAPI
backend (`8765`). No backend changes or CORS expansion were required. See
README for installation, build and launch commands. QML remains reserved for
Phase 8 lightweight native integration, not the full workstation.

The root hook owns the connection; components never open additional sockets.
It validates full snapshots, rejects older generations, retains valid data on
failure and retries with capped backoff. Non-overlapping REST polls every
approximately three seconds recompute freshness in the backend even when the
stream is quiet. Offline/unmount cancels requests, timers and subscriptions.
Disconnected freshness and cached health are explicitly last-known; no frontend
stale thresholds or market calculations are introduced.

NQ history is raw observations, not candles. Up to 1,000 timestamp-sorted
points merge with incoming snapshots, retaining the latest receipt for a
revision and higher value on an equal-receipt tie. Chart time is UTC; header
NY/local time uses timezone-aware browser formatting. Reconnect reloads
history because latest-wins WebSocket snapshots are not a lossless tick feed.
No indicators, macro data, real market feeds, AI, or session calendars exist.

---

### 4.3 Implemented in Phase 2 (partial coverage)

```
BLS / NY Fed / optional FRED       BEA ICS / Fed JSON / Fed RSS
             ↓                                 ↓
       EconomicProvider adapters → EconomicBatch (validated)
             ↓
       EconomicService → SQLiteEconomicRepository
             ↓             immutable versions + provider/series checks
       MacroState / filtered calendar / local history
             ↓
       FastAPI REST + opt-in economic WebSocket invalidations
             ↓
       useEconomics + existing useMarket socket → Macro / Calendar / Overview
```

| Concern | Location | Responsibility |
| --- | --- | --- |
| Domain | `core/domain/economics.py`, `macro_registry.py` | Canonical series, events, UTC timestamps, missing values, importance/status enums, raw surprise |
| Adapters | `core/providers/economic_*.py` | HTTP boundary, normalization, source identities, explicitly selected demo mode |
| Calculations | `core/analysis/economics.py` | Exact-period ratios/differences and input evidence; incomplete window boundaries are not source withdrawals |
| Observed releases | `core/analysis/releases.py` | Deterministic release events from latest stored observation per series: real actual/previous/revised, never a fabricated schedule or consensus |
| Persistence | `core/storage/economics.py` | SQLite append-only semantic versions, namespace isolation, provider and period-aware series checks |
| Service | `core/services/economics.py` | Independent polling, cached state, freshness, calendar windows, coalesced notifications |
| API | `core/api/economics.py` | Macro state/definitions/history, calendar today/week/upcoming/history, provider health |
| Presentation | `web/src/economics/`, `hooks/useEconomics.ts` | Thin grouped pages, histories, coverage warnings, timezone formatting/countdowns |

The existing market provider, market schemas and DuckDB path remain separate.
`create_app(service=..., economic_service=...)` accepts explicit injection;
omitting economics provides disabled responses without constructing providers.
Startup loads persisted data and starts independent provider tasks without
waiting for the network. Shutdown cancels tasks and closes clients/repositories.
SQLite operations are short synchronous transactions on the event-loop thread;
no network await occurs inside a transaction. State updates and notifications
follow successful persistence. Storage failures retain cached data and degrade
health rather than publishing unpersisted revisions.

**History semantics.** Local versions begin at first observation, not the
original official publication. Semantic comparison ignores retrieval timestamps,
but preserves source revisions, actual values, and calculation inputs. A→B→A
creates three versions. Derived history includes complete input coverage;
missing denominators outside a moving source window do not erase saved results.
Explicit missing source values remain null. Per-series checks retain their
latest verified period so truncated older responses cannot refresh newer data.
Live/demo databases are separate and reject mismatched provider provenance.

**Source coverage.** BLS batched API observations, NY Fed reference-rate JSON,
BEA ICS, BEA news-release RSS, Fed calendar JSON and monetary RSS, FRED
observations, and the ForexFactory consensus calendar were fetched successfully
in live mode. 28 macro series are live. Housing, durable goods and regional-Fed
ISM substitutes come from FRED. BLS calendar and the census.gov web pages return
403 (Cloudflare browser challenge); the Census retail headline comes from the
Census data API with `OMARKETS_CENSUS_API_KEY`. The consensus calendar supplies this-week release slots
and market forecasts, but is not a historical schedule backfill. Consensus is a
third-party estimate, is labeled as such, and is never invented; actuals
populate only when the provider supplies them.
The local Stage 2 candidate adds a separate FRED release-calendar adapter for
CPI, Employment Situation and JOLTS slots. It uses source-published Central
times and retains a seven-day lookback for newly enabled calendars. When a
past slot matches a BLS observation for the prior reference month that Omarkets
received after the scheduled time, the calendar can show that current-vintage
value with explicit provenance. It does not assert the original release value
or publication instant. ISM-issued public newswire releases supply Manufacturing
PMI and Prices actuals when the issuer, reference month, 10 a.m. Eastern
publication time, headline and table agree. The consensus calendar row shows
those figures only on an exact time and event match; the stored source records
remain separate. An ADP title containing "Non-Farm" is classified as ADP,
not as the official payroll release. Source identities remain separate. For
coincident CPI, payroll and JOLTS slots, the Stage 2 calendar API candidate's
`display_groups` points to a primary display event and all
related source event IDs, while `events` and stored revisions remain separate.
The web client uses the group only for a single card with expandable source
details; an older API response without groups remains readable.
The Stage E candidate additionally projects typed canonical occurrences with
`occurrence_aliases` and `occurrence_sources`. Same-title ADP corrections within
five minutes on the same New York date share an identity; dates, periods and
weekly/monthly titles remain distinct. BEA notices and schedules reconcile only
with an exact normalized title, explicit reference period and New York date,
with one unambiguous schedule. Publication changes lifecycle, never inventing a
numeric actual. Original IDs resolve to canonical detail; `versions` retains
source revisions. Projection does not rewrite stored events or require a live
migration. Cross-day reschedules and ambiguous matches remain separate.
Verified ISM Manufacturing and Services PMI tables now also emit macro observations
with index-point units, monthly periods, publication times, source links and
content hashes. History starts with verified observations collected after this
adapter change; older event records are retained and not backfilled into macro
history. Matching macro-derived ISM and issuer release records share an occurrence.
The calendar reconstructs older observed members from retained observations when
an issuer occurrence remains, preserving aliases and revision signatures across
period rollover and restart. The observed release feed still selects the newest
period; issuer records alone never manufacture historical observed members.
ISM schedule attachment rejects cancelled slots and conflicting explicit periods.
The ADP issuer adapter reads at most two official listings and eight releases
within a 35-day window. It verifies monthly headline/total/reference month and
weekly NER Pulse headline/table/week ending. Weekly units are jobs per week
(four-week moving average, seasonally adjusted), distinct from monthly jobs.
Monthly publication time comes from the issuer ITEMDATE; weekly time uses the
page publication date and explicit 08:15 Eastern cadence, labeled in provenance.
A unique same-date, same-frequency calendar slot within five minutes receives
the issuer actual, revised previous value, URL evidence and content hash via
canonical occurrence sources. Forecast and original previous remain schedule
values. Legacy ADP titles classified as payrolls are corrected only in projection;
original source records and revisions remain intact. Historical weekly table rows
never become earlier release actuals. Missing/unsupported issuer shapes degrade
provider health and retain unavailable actuals; no estimates or database injection
are used. Polling is every 15 minutes, at most every minute near a known ADP slot.
News ingestion converts summary HTML and entities to readable plain text while
retaining the raw source payload and source URL; it never injects provider HTML.

Actual CPI/core CPI, payrolls, unemployment, wages, JOLTS and claims are also
served as derived **observed releases** (`core/analysis/releases.py`): latest
period per series, real actual/previous/revised_previous, never a fabricated
schedule or consensus, with release time labeled first-observed when the source
has no publication timestamp. Reference-rate series are excluded. BEA/Fed RSS
give real publication times. Future SEP dates are not inferred. Current-vintage
observations are never joined to an old calendar release as if they were original
actuals. The consensus feed is rate-limited; HTTP 429 degrades to an unavailable
check with no retry storm. EIA is stored but unused (Phase 3 energy scope).

**Freshness and polling.** BLS checks daily, NY Fed hourly, BEA/Fed calendars
and FRED every six hours, Fed notices every thirty minutes, consensus hourly. Settings
provide per-provider overrides; persisted checks prevent immediate repeat polls
on restart. Each request has bounded timeout, payload size, and retry attempts;
no adapter TTL cache competes with the service schedule. Source check freshness
is not a publication-completeness or business-session guarantee. Failures retain
cached values, preserving last-success timestamps; optional unconfigured macro
providers do not make healthy calendar sources stale. Release-proximity polling
shortens a provider's interval near its scheduled/observed events and otherwise
restores the normal interval; there is no global multi-process quota management
yet.

**REST and push.** `/api/v1/macro/state`, `/macro/series`, `/macro/series/{id}`,
`/calendar`, `/calendar/today`, `/calendar/week`, `/calendar/upcoming`,
`/calendar/{id}`, and `/providers` are under `/api/v1`. Calendar filters use UTC
aware half-open ranges; today/week boundaries use `America/New_York` including
23/25-hour DST days. Publication-only notices use `released_at` for listing,
without fabricating a schedule. `/health` includes economic provider health.

`/ws/market` stays market-only unless `topics=macro` is requested. The opted-in
single connection multiplexes the six economic notification types:
`economic_event_upcoming`, `economic_event_due`, `economic_event_released`,
`economic_event_revised`, `macro_series_updated`, `provider_status_changed`.
Payloads contain changed IDs and occurrence time, not database snapshots.
Bounded per-type ID unions coalesce changes; overflow signals `ids=["*"]`.
REST is the recovery source, not a lossless notification stream. Frontend
invalidation refreshes are debounced and non-overlapping with ~30-second polls;
reconnect and offline cancellation preserve qualified last-known data. No
second socket, provider calls, or surprise calculations exist in the UI.

See README for configuration, supported series, source limitations, launch,
CLI and testing commands. No Phase 3 market feeds or AI interpretation were added.

---

### 4.4 Implemented in Phase 3 (real market data)

```
CNBC quote webservice ──┐
                        ├─→ CompositeMarketDataProvider → MarketStateService → DuckDB
Binance spot ───────────┘        (per-observation source + feed status)
                                        ↓
                          build_market_state (feed_status, provider_symbol,
                          contract_code, latency) → REST + /ws/market → UI
```

| Concern | Location | Responsibility |
| --- | --- | --- |
| Futures contracts | `core/domain/contracts.py` | Front-month selection (NQ/ES: H/M/U/Z, third Friday; WTI: monthly NYMEX CL rule), roll rule, contract code/expiry/provider symbol |
| Feed status | `core/domain/models.py` | `FeedStatus` (live/delayed/stale/unavailable/demo) on `InstrumentState`, plus provider symbol, contract, latency |
| CNBC adapter | `core/providers/cnbc.py` | Canonical↔vendor mapping, yield normalization to percent, delayed-vs-live from exchange lag, bounded poll with backoff |
| Binance adapter | `core/providers/binance.py` | BTC spot, bounded poll with backoff |
| Composite | `core/providers/composite.py` | Merge child streams; one failing child cannot stop the others; single start/close per child |
| Routing | `core/providers/registry.py` | Canonical symbol → provider, configurable, no scattered conditionals |
| State | `core/services/market_state.py` | Feed classification from timestamps, never guesswork; delayed stays delayed |
| API/CLI | `core/api/routes.py`, `core/cli.py` | `/markets/status`, `/market/providers`, `/contracts`; `omarketsctl markets|quote|contracts|market-providers` |
| UI | `web/src/App.tsx`, `format.ts` | Global data-mode indicator, per-instrument feed label, source/contract detail panel |

**Mode.** `OMARKETS_MARKET_DATA_MODE` is `live` (default) or `demo`. Demo uses
only the synthetic provider. Live composes real adapters from the routing table.
A failed live feed degrades to `unavailable`/`stale`; it is never replaced by
demo data. `MARKET_DATA_MODE`/`data_provider` allow explicit adapter overrides.

**Delayed vs live.** The adapter computes exchange-timestamp lag against receipt.
In-session lag above `OMARKETS_MARKET_LIVE_LAG_SECONDS` (default 60s) is
`DELAYED`; a date-only value is `STALE` (exact exchange time unavailable) and is
never given an invented time; an out-of-session value is `STALE`. A delayed feed
stays `DELAYED` even past the staleness threshold — the delay is a source
property, not a fault. Verified: CNBC NQ/ES lag ~10 minutes (delayed), while
yields/DXY/BTC update in seconds (live).

**Units.** Treasury yields are canonical **percent** (e.g. `4.922`), never
fractions (`0.04922`) or basis points; the UI converts change to bp for display
only. VIX/VXN are spot indices (not futures). DXY is the ICE U.S. Dollar Index.
Contract selection is deterministic; there is no back-adjusted series.

No paid vendors, historical tick download, or vendor websocket is used. The NQ
chart is a real-observation line accumulated from polling, not vendor OHLC.

---

### 4.5 Implemented in Phase 4 (deterministic technical & cross-market state)

```
MarketStateService (observations)
        │ subscribe_observations()
        ▼
TechnicalService ── bar aggregation (core/ingest/bars) ── session engine (core/domain/sessions)
        │                                                    │
        │                                                    ▼
        │                            pure analysis (core/analysis/technical, cross_market)
        │                                                    │
        ▼                                                    ▼
TechnicalMailbox  ←  coalesced typed events          TechnicalState / CrossMarketState
        │                                                    │
        ▼                                                    ▼
/ws/market?topics=technical                        REST + Technical UI + Overview context
```

| Concern | Location | Responsibility |
| --- | --- | --- |
| Bar model | `core/domain/bars.py` | `Bar` OHLCV (optional volume), `BarInterval`, `origin` |
| Session engine | `core/domain/sessions.py` | Globex/RTH/maintenance/weekend, DST-safe, prior day/week |
| Technical schemas | `core/domain/technical.py` | `TechnicalState`, `CrossMarketState`, `Value` availability |
| Bar aggregation | `core/ingest/bars.py` | Observation→bar aggregation, cumulative-volume differencing, resampling |
| Calculations | `core/analysis/technical.py`, `core/analysis/cross_market.py` | Pure levels/VWAP/swings/structure/BOS/ATR/vol and NQ-vs-ES/yields/DXY/VIX |
| Service | `core/services/technical.py` | Observation subscription, bar window, recompute, event fan-out |
| API | `core/api/technical.py` | `/technical/*`, `/cross-market`, `?topics=technical` |
| Configuration | `core/config/settings.py` | `technical_enabled`, `technical_primary_interval` |
| Presentation | `web/src/technical/`, `hooks/useTechnical.ts` | Technical page, overlays, Overview context |

**Bars are derived.** Phase 3 providers supply quotes, not bars, so Omarkets
aggregates bars from the polled observation stream and marks them
`origin=aggregated_observations`. A 1m bar is sampled, so its high/low are lower
bounds, not exchange OHLC. Completed bars are in-memory only and are re-derived
from stored observations on restart; no new database table is introduced, so
`observations` remains the single durable source of truth.

Intraday calculation version 2 finalizes a bucket only when received observations
advance the exchange watermark beyond its end. The evaluation cutoff filters
both exchange and receipt times before revision selection. Resampling emits
partial windows with `complete=false`, `missing_intervals`, and a completion
reason; only contiguous complete source intervals finalize a larger window.
`observed_through`, `received_through`, and `completion_received_at` retain the
sample and closure evidence. Legacy bars default to version 1 and are excluded
from current resampling. Daily/weekly bars use version 2: every tradable minute
must have a sample, and a received exchange watermark must reach the final
session close. Holiday halts and closed days are excluded; missing minutes or
sessions remain explicit partial coverage. Wall-clock close alone never finalizes
a bar; highs and lows remain sampled even when coverage is complete.

Historical event reaction policy 3 measures received delayed 1m/5m/15m samples
using the same timestamp and baseline qualifications as other feeds. A delayed
stream below its endpoint reports `measurement_status=waiting`; a missing
endpoint after the stream passes it remains unavailable. Policies 1/2 remain
readable historical records but are excluded from current analyst/briefing
horizon evidence. Context signatures use `ctx-v3`; semantic inference identity
is unchanged so this repair does not trigger a paid reanalysis sweep.

**Volume and VWAP.** CNBC supplies cumulative session volume for NQ/ES, which is
differenced within a session/contract. Volume is `null` — and VWAP `unavailable`
— when volume is absent, when the cumulative series decreases mid-session, or
when the session open was not observed. A simple average is never substituted
for VWAP.

**Look-ahead safety.** A pivot is emitted only once its right-hand window is
complete and carries both `time` and `confirmed_at` (the completion time of the
confirming bar). Only complete bars participate in pivot detection, so a pivot
cannot appear and then repaint. Structure classification and break-of-structure
use only confirmed pivots; BOS requires a closed bar, is anchored to the first
bar after confirmation that exceeded the level, and is a pure function of closed
bars, so recompute cannot duplicate or drift an event. The service also
recomputes on a timer even when the feed is quiet, so an idle or stalled feed
ages into `stale` instead of being served as live indefinitely.

**Sessions.** `core/domain/sessions.py` localizes wall-clock ET per date via
`zoneinfo`, so DST never depends on a fixed offset. Exchange holidays and
half-days are not modeled; a holiday weekday yields empty windows and
`insufficient_data` rather than invented levels.

**Availability.** Every computed scalar is `available` / `unavailable` /
`insufficient_data` with a reason, so the API and UI can never render missing
data as zero. Technical state carries the underlying feed status and a `stale`
flag and inherits the feed's freshness.

**Boundaries.** No LLM touches a calculation. `core/analysis` stays pure;
`core/ingest` owns normalization; the UI renders only. No persisted bar table,
holiday calendar, multi-timeframe structure, back-adjusted series, or trade
signal is added in Phase 4.

---

### 4.6 Implemented in Phase 5 (news, economic-calendar and geopolitical ingestion)

```
Public/official feeds (RSS/Atom)      Phase 2 economic calendar
        │                                     │
        ▼                                     ▼
NewsSource adapters (core/providers/news) ── EconomicCalendarSource
        │  SourceItem (typed, vendor-free)
        ▼
core/ingest: normalize → deterministic entities/instruments → dedupe
        │  MarketEvent + SourceOccurrence + RawEvent
        ▼
SQLiteEventRepository  (raw / canonical / occurrences + link tables)
        │
        ▼
NewsService  ──► coalesced news invalidations ──► /ws/market?topics=news
        │  register_processor(event) — the Phase 6 seam
        ▼
REST /api/v1/events, /sources  ──►  News/Events UI page
```

| Concern | Location | Responsibility |
| --- | --- | --- |
| Domain | `core/domain/news.py` | `MarketEvent`, `SourceItem`, `SourceOccurrence`, `RawEvent`, `SourceHealth`, source/entity/status enums |
| Source adapters | `core/providers/news/` | `NewsSource`/`HttpNewsSource` interface, RSS/Atom parser, central-bank metadata, catalog, economic-calendar bridge |
| Extraction | `core/ingest/entities.py` | Deterministic country/region/institution/instrument/company recognition and event-type keyword rules |
| Normalization | `core/ingest/normalize.py` | Shared SourceItem→MarketEvent normalization, UTC handling, dedup key |
| Deduplication | `core/ingest/dedupe.py` | Exact key + conservative token-similarity near-duplicate matching |
| Persistence | `core/storage/events.py` | Raw payloads, canonical events, occurrences, indexed instrument/country links, source health |
| Service | `core/services/news.py` | Per-source scheduling, failure isolation, dedup/resolve, event bus, processors, stats |
| API | `core/api/news.py`, `core/api/websocket.py` | `/events`, `/sources`, news WebSocket topic, health |
| Configuration | `core/config/settings.py` | `news_enabled`, poll intervals/overrides, source allow/deny lists, dedupe window |
| Presentation | `web/src/news/`, `hooks/useNews.ts` | News/Events feed, economic-calendar table, source-health panel |

**Deterministic only.** Phase 5 is `SOURCE → FETCH → NORMALIZE → DEDUPLICATE →
STORE → STREAM`. There is no LLM call anywhere in this path. `event_type` is a
source-provided category or a deterministic keyword mapping, never a model
judgement. Relevance, novelty, materiality, market classification, urgency and
IGNORE/STORE/ALERT/ESCALATE routing are explicitly deferred to Phase 6
(TypeSafe Jev).

**Canonical vs source occurrences.** The same real event reported by several
feeds yields one canonical `MarketEvent` (with merged `sources` and
`occurrence_count`) plus one `SourceOccurrence` per report. Exact
deduplication-key matches are windowed in time so a recurring headline does not
merge across days; near-duplicates additionally require matching
instruments/countries and token similarity above a configurable threshold. Raw
payloads are persisted verbatim and never destroyed by normalization.

**Adapters and isolation.** Each source is an independent adapter polled on its
own schedule with bounded timeouts, one deferred retry, payload-size limits, and
per-source health (`healthy`/`degraded`/`offline`/`disabled`). A failing source
records its error and cannot stop the others. A source is polled every
`OMARKETS_NEWS_POLL_SECONDS` or its slower catalog `poll_seconds`, whichever is
longer (e.g. Department of Energy 15 min, European Commission 10 min); explicit
overrides win. Rejections name the HTTP status. HTTP 429 is never retried at once:
the source waits `Retry-After`, or 15 minutes without one (at most 6 hours), and
a 5xx `Retry-After` over 30 s also defers the next poll. A web page (HTML) where
a feed was expected, or broken XML, is a clean failure for that poll; stored
items are kept. Parsing uses `defusedxml` (no DTDs or external entities). Credentialed sources are declared
but disabled until a key is configured; no key is hard-coded.

**The Phase 6 interface.** `NewsService.register_processor(event)` receives each
newly normalized, deduplicated, persisted canonical event, and
`subscribe_events()` offers an in-process snapshot; the `?topics=news` WebSocket
topic publishes coalesced `market_event_created` / `market_event_updated` /
`source_status_changed` ids. Phase 5 registers no classifier, so Phase 6 can
attach without rewriting the pipeline.

**Boundaries.** The UI never opens the event database or calls a feed; it reads
REST/WebSocket only. The economics bridge adapts Phase 2 events and recomputes
nothing. No relevance/sentiment score, article scraping, or LLM call is added in
Phase 5.

---

### 4.7 Implemented in Phase 6 (TypeSafe Jev materiality & decision routing)

```
Phase 5 canonical event
        │  register_processor(enqueue)  ← synchronous, no I/O
        ▼
DecisionService queue ── async worker ──► core/analysis/decision_context (pure)
        │                                       │  event + macro + market
        │                                       │  + technical + reaction windows
        │                                       ▼
        │                              TypeSafeJevProvider (TypeSafe System One)
        │                                       │  typed noul/choice/score answers
        │                                       ▼
        │                       materiality engine (pure) + router (pure)
        │                                       │
        ├──► decisions / materiality / routing / escalations / usage (SQLite)
        └──► ?topics=decisions invalidations ──► News UI
```

| Concern | Location | Responsibility |
| --- | --- | --- |
| Domain | `core/domain/decisions.py` | `EventDecision`, `MaterialityState`, `RoutingDecision`, `EscalationRequest`, reason codes, enums |
| Context models | `core/domain/decision_context.py` | `DecisionContext`, `ReactionWindow`, macro/market/technical sub-context |
| Provider | `core/providers/decisions/` | `DecisionProvider` ABC, Jev transport models, TypeSafe adapter, fake provider, versioned question set |
| Context builder | `core/analysis/decision_context.py` | Pure, time-disciplined reaction measurement and context assembly |
| Materiality | `core/analysis/materiality.py` | Pure weighted engine, policy, unit-aware surprise |
| Routing | `core/analysis/routing.py` | Pure router, high-impact overrides, escalation priority, analyst targets |
| Storage | `core/storage/decisions.py` | Append-only decisions/materiality/routing/escalations/provider checks/usage |
| Service | `core/services/decisions.py` | Async worker, synchronous enqueue seam, backfill, cache, bounded re-evaluation, failure isolation, event bus |
| API | `core/api/decisions.py`, `core/api/websocket.py` | `/decisions*`, `/escalations`, `/ai/*`, `?topics=decisions` |
| Configuration | `core/config/settings.py` | `decision_*`, `typesafe_api_key` |
| Presentation | `web/src/news/News.tsx`, `hooks/useDecisions.ts` | Decision detail, materiality filters, escalation state, provider/usage panel |

**Deterministic vs Jev.** Jev classifies, scores and routes. It never generates
prose and never computes a value: reaction windows, changes, basis points,
surprises and structure breaks are all measured deterministically before the
model is called. The materiality engine combines those facts with Jev beliefs and
emits named reason codes; there is no single opaque AI score. Omarkets routing —
not the model — decides actions.

**The noul/confidence distinction.** A `noul` answer is P(yes), a belief. It is
stored as `probability` with a derived `magnitude`; it is never treated as the
`confidence` that `choice`/`score` return. Conflating the two is a correctness
bug the schema and tests prevent.

**Async seam.** The Phase 5 processor is synchronous and runs on the event-loop
thread, so `DecisionService.enqueue` only queues. All provider I/O, storage and
re-evaluation happen on the service's own task; ingestion is never blocked.

**Time discipline.** The reaction baseline is the last observation strictly
before the event anchor by **exchange** timestamp; a delayed feed yields
`insufficient_data` rather than a computed reaction; snapshot-now market/technical
context is attached only when the anchor is close to now, so backfill cannot
misattribute a later structure break to an old event.

**Fail closed.** Any provider failure, missing key, malformed answer or
context-build fault is persisted as an `unavailable` decision with a required
`error`, never guessed, and never cached as if it were a result. A bounded
re-evaluation tick plus startup backfill retry failures and re-decide when
reaction data appears; re-evaluation is horizon-bounded so a closed-market event
does not retry forever.

**Auditability.** Decisions are append-only and versioned by
`(decision_schema_version, question_set_version, context_signature)` and record
the model that produced them, so a future model upgrade does not rewrite history.

**The Phase 7 interface.** Escalated events are stored as `EscalationRequest`
rows in a `pending` state with priority, analyst targets and context signature.
Phase 6 registers no analyst and never invokes one; Phase 7 consumes the queue.

**Boundaries.** No LLM touches a deterministic calculation; `core/analysis` stays
pure; the provider adapter is the only place TypeSafe exists; the UI renders
decisions and never re-derives them. No analyst call, market summary, trade
signal, notification dispatch, or Hermes orchestration is added in Phase 6.

---

### 4.8 Implemented in Phase 7 (native analyst layer, PydanticAI + Ollama Cloud)

```
Phase 6 escalation (pending)
        │  escalation source + polling worker
        ▼
AnalystService ── claim (durable, idempotent) ──► core/storage/analyses.py
        │
        ▼
core/ai/context (pure) ── per-analyst compact AnalysisContext + evidence refs
        │
        ▼
Dispatcher (deterministic plan) ── bounded asyncio concurrency ──► specialists
        │
        ▼
PydanticAI Agent (no tools, output_type=AnalystOutput) ──► Ollama Cloud
        │
        ▼
deterministic validation (schema + evidence grounding) ──► AnalystResult
        │
        ├── synthesis policy (deterministic) ──► optional synthesis
        ├── append-only storage
        └── ?topics=analysts invalidations ──► Analysts UI + event detail
```

| Concern | Location | Responsibility |
| --- | --- | --- |
| Result schemas | `core/domain/analysis.py` | `AnalystOutput`, `AnalystResult`, `AnalysisStatus`, `ContextQuality`, usage |
| Analyst context | `core/domain/analyst_context.py`, `core/ai/context.py` | Per-analyst compact context and stable evidence refs; pure, no I/O |
| Prompts | `core/ai/prompts.py` | Versioned system instructions; untrusted content fenced separately |
| Policy | `core/ai/policy.py` | Specialists, synthesis decision, model selection, context gating |
| Dispatcher | `core/ai/dispatcher.py` | Deterministic fan-out/fan-in with bounded concurrency (plain asyncio) |
| Provider | `core/ai/providers/` | `AnalystModelProvider`, fake provider, Ollama Cloud via PydanticAI |
| Validation | `core/ai/validation.py` | Unsupported evidence refs and possible unsupported numeric claims |
| Storage | `core/storage/analyses.py` | Append-only analyses + escalation job lifecycle + usage |
| Service | `core/services/analysts.py` | Queue consumption, claiming, retries, synthesis, event bus |
| API | `core/api/analyses.py`, `core/api/websocket.py` | `/analyses*`, `/analysts/*`, `?topics=analysts` |
| Configuration | `core/config/settings.py` | `analyst_*`, `ollama_api_key` |
| Presentation | `web/src/analyst/`, `hooks/useAnalyses.ts`, `news/News.tsx` | Analysts page, engine/usage panels, event analysis status |

**Analyst context and evidence.** Each analyst receives only the sections it
needs. Every meaningful value has a stable reference id; the prompt renders the
context and lists the evidence. The model cites ids, never free-form citations,
and a deterministic post-validator flags any cited id that was not supplied or a
numeric token matching no supplied value.

**Escalation lifecycle.** Jobs move `pending → claimed → analyzing → completed`,
with `retryable_failure`, `permanent_failure` and `insufficient_context` variants.
Claiming is a durable CAS, so two workers cannot analyze the same escalation and
a restart does not duplicate work; a stale claim left by a crash is recovered as
pending. A completed analysis whose context signature is unchanged is reused;
a materially changed context creates a new version and the previous result is
retained.

**Concurrency and backpressure.** Specialists run concurrently under a semaphore
bounded by `analyst_max_concurrency`; escalation priority orders the queue.
Queue depth, active count, oldest-pending age and today's completed/failed counts
are exposed through `/analysts/queue` and `/health`.

**Synthesis.** Deterministic policy decides synthesis: an explicit Phase 6
`synthesis` target, two or more complementary domains, CRITICAL materiality, or
material disagreement. A single specialist is returned directly; synthesis costs
tokens and is not run after every event. A stronger configured synthesis model is
used only when the synthesis is justified as complex.

**Prompt injection and tools.** Event/source content is untrusted data placed in
the user message behind an explicit boundary; the system instruction tells the
model to ignore embedded instructions. Analysts have no shell, browser,
filesystem, network or code-execution tools in this phase.

**Boundaries.** `core/analysis` stays pure; `core/ai` stays non-deterministic
interpretation and never computes a fact; PydanticAI is used only as a typed
single-shot agent; the provider adapter is the only place Ollama exists; the UI
renders results and never re-derives them. No trade signal, notification
dispatch, or Hermes orchestration is added in Phase 7.

---

### 4.9 Implemented in Phase 8 (native Omarchy integration)

```
Phase 6 decision bus (ALERT_CANDIDATE / ESCALATED)
        │
        ▼
NotificationService ── pure policy (core/analysis/notifications.py)
        │                    → NotificationPlan (or None)
        │ dedupe by event id
        ▼
NotifySendNotifier ──► notify-send ──► Omarchy notification daemon

Omarkets API (127.0.0.1:8765)
        │  update.py (stdlib, fixed URL, data only)
        ▼
$XDG_STATE_HOME/omarkets/bar.json
        │
        ▼
Quickshell plugin (omarchy/omarkets/) ──► bar widget + compact popup
```

| Concern | Location | Responsibility |
| --- | --- | --- |
| Plan schemas | `core/domain/notifications.py` | `NotificationPlan`, urgency, kind, dedupe key |
| Policy | `core/analysis/notifications.py` | Pure "should this notify, and how" decision; alert, escalation, release and analyst-completion plans |
| Delivery | `core/notifications/notifier.py` | The only `notify-send` boundary; fixed argv |
| Service | `core/services/notifications.py` | Decision **and** analyst bus consumer, persistent dedupe, failure isolation |
| Notification store | `core/storage/notifications.py` | Delivered-key records, so a restart does not re-notify a backfill |
| Shell schemas | `core/domain/shell.py` | Versioned, presentation-oriented `ShellStatus` / `ShellContext` |
| Shell projection | `core/api/shell_view.py` | Pure builders from already-computed state; no calculation |
| Shell API | `core/api/shell.py` | `/api/v1/shell/status`, `/api/v1/shell/context` (read-only, safe defaults) |
| Health | `core/api/routes.py`, `core/api/schemas.py` | `/api/v1/notifications/status`, health block (data vs AI) |
| Bar collector | `omarchy/omarkets/update.py` | Fetches one shell endpoint into `bar.json` (stdlib, atomic) |
| Shell connection | `omarchy/omarkets/OmarketsService.qml` | One poller/FileView; widgets consume it (no per-widget clients) |
| Bar widget / popup | `omarchy/omarkets/BarWidget.qml`, `Panel.qml` | Presentation only; the bar is a compact launcher icon, the popup holds the market detail |
| Launcher | `omarchy/omarkets-launch` | Opens or **focuses** the workstation; deep links via `?section=` |
| User service | `omarchy/omarkets.service` | `systemd --user` unit; the single owner of the backend process |
| Install / remove | `omarchy/install-plugin`, `omarchy/uninstall-plugin` | Idempotent, backed-up, reversible |

**Shell transport.** The installed Quickshell `Io` module exposes `FileView` but
no WebSocket type, so the shell polls. `update.py` fetches ONE versioned,
normalized endpoint (`/api/v1/shell/status`), writes `bar.json` atomically, and
the QML `OmarketsService` watches it. The core owns normalization, timezone
formatting and data-vs-AI health so QML never assembles internal models.
The optional `session.next_open_at` is computed in UTC by the exchange session
engine when Globex is closed or in maintenance. During those phases CNBC quote
polling and routine technical/Jev/analyst/briefing work pause; calendar and news
poll hourly and wake at the exact next session open. The API and stored state
remain readable. Market writes run in a worker so synchronous DuckDB inserts
do not occupy the API event loop; bounded duration logs identify remaining
slow operations before attributing a stall to a single source.

**Thin UI.** The plugin computes nothing. It reads one JSON file the collector
wrote; every value, feed status and health badge comes from the core. A stale
last-known file is detected from the collector's own `updatedAt` and rendered as
offline, never as live. The bar widget opens the popup through the shell's own
bar-widget panel routing (`omarchy-shell shell toggle omarkets`), not a private
IPC target. `?section=` deep links let the popup and hotkeys open specific views.

**Deterministic notification policy.** Phase 6 already marks alert candidates;
Phase 8 decides delivery from the materiality and routing the core computed. LOW
never notifies; a bare medium escalation is suppressed; CRITICAL is urgent.
Economic-release notifications carry deterministic actual/forecast/previous and
key per `(series, period)`, so many feeds yield one notification. Analyst
completions notify only at HIGH/CRITICAL with a synthesis or material
disagreement. The policy is pure and unit-tested without a desktop. Delivered
keys are persisted, so a restart does not re-notify a backfill.

**Process lifecycle.** The backend and the analyst worker are one process, so
they run as ONE `systemd --user` unit (`omarkets.service`, `Restart=on-failure`,
bounded `StartLimitBurst`). The same process serves the built web workstation as
static files, so no separate frontend server is required in production.

**Failure isolation.** A missing `notify-send`, a non-graphical session, a
delivery error, an unreachable backend, malformed JSON, a provider outage or a
dead analyst layer each degrade to an explicit state without crashing the shell
or the core. Data health and AI health are reported separately, so a dead Jev or
Ollama layer never makes healthy market data read as down. Delivery text is
passed as fixed argv entries, so notification content is never shell-interpreted.

**Boundaries.** No market logic in QML; the plugin never opens a database or
calls a provider; the notification service never recomputes an importance score;
no trade signal is added in Phase 8; no secret ever reaches QML.

---

### 4.10 Implemented in Phase 9 (Qwen voice + Omarkets interface)

```
                         USER
                           │
                        VOICE
                           │
                 Qwen Audio Realtime
                           │
                     Frontend MCP
                           │
       ┌───────────────────┼───────────────────┐
       ▼                   ▼                   ▼
 Omarchy Control       Omarkets MCP       Coding Agents
 apps/browser/etc           │             Hermes/OpenCode/
                           │                 Codex
                           ▼
                       Omarkets
                           │
       ┌───────────────────┼─────────────────────┐
       │                   │                     │
       ▼                   ▼                     ▼
 Market State       Jev Decisions        Analyst Results
 Macro/Calendar      Materiality           Ollama Cloud
 Technical           Events                Synthesis
```

| Concern | Location | Responsibility |
| --- | --- | --- |
| MCP server | `core/interfaces/mcp/server.py` | Stdlib-only stdio JSON-RPC; read-only voice tool surface |
| Voice projections | `core/interfaces/mcp/tools.py` | Pure payload→spoken projections; no I/O, no calculation |
| HTTP boundary | `core/interfaces/mcp/client.py` | Loopback-only API client; fail-fast errors |
| Launcher | `core/interfaces/mcp/launcher.py` | Delegates to the Phase 8 `omarkets-launch` (focus, not duplicate) |
| Entrypoint | `core/interfaces/mcp/__main__.py`, `omarchy/omarkets-mcp` | `python3 -m core.interfaces.mcp`; venv-free launcher |
| Qwen surfaces | `frontend-mcp.json`, `PROMPT.md` | Allowlist entry + routing rules in the Qwen project |

**Qwen is ears, routing and presentation.** The realtime model converts
structured results into concise speech; it must not reconstruct market analysis,
browse for alternate answers, or compute a market value. Every tool returns a
`spoken` string already phrased for speech and a `stale` flag, so the assistant
cannot confidently read delayed data as live.

**Integration direction.** `Qwen frontend → Omarkets MCP adapter → Omarkets
localhost API`. The adapter never imports Omarkets storage, analysis or service
internals and never opens DuckDB; Omarkets business logic stays in the core. The
API remains the single source of truth.

**Tool surface.** User-level intents, not one tool per endpoint:
`market_status`, `market_brief`, `market_next_event`, `market_calendar`,
`market_macro`, `market_technical`, `market_latest_events`,
`market_latest_analysis`, `market_event_analysis`, `market_usage`,
`open_omarkets`. All are read-only: retrieving state, analyses, events, usage,
and launching/focusing the UI. No tool edits configuration, thresholds, model
policy, credentials or databases, and there is no generic shell/file/URL tool.

**No unnecessary model invocation.** A status or calendar question retrieves
current state; it never triggers Jev, Ollama or a specialist analyst. The
`market_brief` tool reuses existing stored analysis, and when none is present it
returns deterministic state rather than starting a new model call. A manual
analysis request, if ever added, must go through the existing escalation queue
and analyst service — never a direct Ollama call from the voice layer.

**Three required surfaces.** In the Qwen project a tool is not shipped until the
MCP server definition, the frontend allowlist (`frontend-mcp.json`) and the
routing prompt (`PROMPT.md`) all agree. The Qwen project's
`tests/test_omarkets_surface.py` pins all
three, including that `PROMPT.md` names every tool and stays under the loader's
16,000-character cap (a truncated prompt would silently drop the Omarkets rules).

**Failure handling.** Omarkets offline yields a short "Omarkets isn't responding"
tool error; the voice assistant keeps working for its normal desktop tasks.
Stale data is flagged and spoken. A dead Jev or Ollama layer leaves market, macro
and technical reads working. Qwen's own frontend MCP being unavailable leaves the
desktop tools unaffected. A tool failure never crashes the MCP connection.

**Boundaries.** `core/analysis` stays pure; `core/ai` stays non-deterministic
interpretation; the MCP adapter is presentation-only and read-only; no trade
signal, order execution, voice-editing of Omarkets configuration, or
modification inside the Qwen realtime audio loop is added in Phase 9.

---

### 4.11 Implemented in Phase 10 (daily briefings + workflow automation)

```
Normalized economic calendar (EconomicEvent.scheduled_at)
        │
        ▼
core/workflows/policy  (pure: classify → profile offsets → eligibility/fingerprint)
        │
        ▼
core/workflows/service  (scheduler → due triggers → dedupe → grace windows)
        │
        ├── deterministic state (market / technical / cross-market)   core.services
        ├── existing stored analysis (reuse first)                    core.services.analysts
        ├── fresh analysis only when justified → escalation queue      Phase 7 path
        │
        ▼
core/workflows/briefing  (pure sections + evidence refs)
        │
        ▼
SQLiteBriefingRepository (append-only briefings + workflow_runs)
        │
        ├── notifications (Phase 8 boundary)     ├── REST / WS ?topics=briefings
        └── web Briefings page                    └── Qwen market_latest_briefing
```

| Concern | Location | Responsibility |
| --- | --- | --- |
| Domain | `core/domain/briefings.py` | `Briefing`, `BriefingSection`, `BriefingItem`, `WorkflowRun`, `WorkflowDefinition`, `CalendarFinding`, enums |
| Policy | `core/workflows/policy.py` | Pure event classification, behaviour profiles (offsets), eligibility, context fingerprint, follow-up justification, cooldowns/grace windows |
| Briefing assembly | `core/workflows/briefing.py` | Pure builders for each briefing type; reuses Phase 6 reaction machinery |
| Scheduler/service | `core/workflows/service.py` | Async worker, due-trigger computation, dedupe, reuse, bounded analysis wait, notification, bus |
| Reconciliation | `core/workflows/reconciliation.py` | Deterministic calendar validation (duplicates, mismatches, gaps) |
| Storage | `core/storage/briefings.py` | Namespaced SQLite; append-only versioned briefings + deduplicated runs |
| API | `core/api/briefings.py` | `/briefings*`, `/workflows/status`, `/workflows/run`, `/workflows/reconcile` |
| Config | `core/config/settings.py` | `workflow_*` flags, leads, grace windows, cooldowns, budget, notification toggles |
| Presentation | `web/src/briefings/`, `hooks/useBriefings.ts` | Briefings page + workflow panel; today's calendar via the existing Calendar page |
| Voice | `core/interfaces/mcp/` `market_latest_briefing` | Retrieval of a prepared briefing; never a new analysis |

**Calendar authority.** The scheduler reads only the normalized calendar produced
by `core.services.economics` (`calendar_events()` + `observed_releases()`) and
schedules relative to `scheduled_at`. No release clock time or date is encoded in
workflow logic. Profiles are offsets; classification keys on `event_code` and
`category`. Derived observed releases have no `scheduled_at` and are therefore
never scheduled (they feed the decision/news path, not event workflows).

**Reaction discipline.** A post-release briefing reuses
`core.analysis.decision_context.build_reaction` with the anchor set to the
event's `scheduled_at`: only observations strictly before the anchor form a
baseline and only in-window samples after it count, so pre-release movement can
never be measured as reaction. A short window on a known-delayed feed is
`insufficient_data`, never zero.

**FOMC.** The statement and press conference are distinct calendar events with
distinct workflows (`fomc_statement_reaction`, `fomc_press_conference_reaction`).
The press stage is produced only when the calendar explicitly represents a
press-conference event; Omarkets never invents one from a `+30m` assumption.

**Analysis and cost.** Stored analysis is reused when it exists for the event;
otherwise fresh work goes through the Phase 7 escalation queue with a bounded
wait, and a briefing is still produced (`pending`) if analysis is slow. The legacy
`workflow_daily_ai_budget` and critical-exception settings are deprecated and
ignored; workflow request counts do not suppress funded work. Unique admissions
remain recorded for history. Provider admission still enforces the unchanged
monthly/daily dollar limits, reservations, provider blocks and recovery policy.
Every run records reuse vs. fresh.

Pre-market and material-context requests receive the available stored macro
series with original period, source, timestamp and stale qualification. These
series are explicitly current-vintage background, never original release actuals
or surprises. Macro freshness uses the stored series status rather than the
30-minute quote window. Future receipts/publications are excluded. Both workflow
contexts include recent supplied technical/cross-market state and dated calendar
catalysts with evidence references; current-vintage calendar actuals retain their
receipt time and qualification.

**Reliability.** Durable dedup keys prevent duplicate runs across restart, a
duplicate calendar entry, a scheduler race or overlapping sources. Explicit
configurable grace windows decide whether a missed run still executes;
`calendar_reconciliation` validates the calendar and flags discrepancies (it
never rewrites or invents events). The service is a single in-process asyncio
task started/stopped in the FastAPI lifespan, so it cannot block market-data
services and shares the single-writer SQLite discipline.

**Boundaries.** `core/workflows` orchestrates only; it computes no market fact,
calls no model directly, and does not touch Qwen's realtime loop. `core/analysis`
stays pure. Briefings are structured data separated from every renderer (web,
notification, voice).

---

### 4.12 Implemented in Phase 11 (historical intelligence + calibration)

```
anchor (event / decision / analysis / FOMC stage)
        │
        ▼
HistoricalFeatureSnapshot      ← what Omarkets knew at the anchor (point-in-time)
        │
        ▼ (horizon passes)
deterministic outcome          ← what happened after, from stored observations
        │
        ▼
SQLiteHistoricalRepository (outcomes / assessments / evaluations / reports)
        │
        ├── cohort statistics (typed, parameterized, sample-gated)
        ├── calibration reports (materiality / Jev / analysts / FOMC)
        └── REST + WS ?topics=historical + web History page + Qwen tools
```

| Concern | Location | Responsibility |
| --- | --- | --- |
| Market calendar | `core/domain/market_calendar.py` | Rule-computed NYSE holidays + early closes; no yearly list |
| Session integration | `core/domain/sessions.py` | Holiday/early-close-aware sessions, phases, trade-date navigation |
| Domain | `core/domain/historical.py` | `HistoricalOutcome`, `HistoricalFeatureSnapshot`, `HistoricalAssessment`, `HistoricalEvaluation`, `CalibrationReport`, `CohortQuery/Result`, enums |
| Anchors/policies | `core/historical/anchors.py` | Anchor types, horizon policies, stable outcome identity |
| Outcomes | `core/historical/outcomes.py` | Pure measurement, no-lookahead, persistence labels, cross-asset/technical outcomes |
| Snapshots | `core/historical/snapshots.py` | Point-in-time feature snapshots |
| Cohorts | `core/historical/cohorts.py` | Typed filters → parameterized SQL, robust statistics, Wilson CI, readiness |
| Calibration | `core/historical/calibration.py` | Materiality/Jev reports, reliability bins, Brier/ECE for genuine probabilities |
| Service | `core/historical/service.py` | Anchor discovery, bounded/resumable backfill, cohort + calibration, bus |
| Storage | `core/storage/historical.py` | Namespaced SQLite; idempotent outcomes by `(anchor, instrument, horizon)` |
| API | `core/api/historical.py` | `/historical/*` read-only + typed query/backfill |
| CLI | `core/cli.py` | `historical`, `outcomes`, `calibration`, `calibration-jev`, `historical-query`, `historical-fomc`, `historical-backfill` |
| Presentation | `web/src/history/`, `hooks/useHistorical.ts` | History section: calibration overview, outcome table, query explorer, FOMC/Jev |
| Voice | `core/interfaces/mcp/` | `market_history_query`, `market_materiality_calibration`, `market_fomc_history` |

**Point-in-time integrity.** A snapshot records the event revision, materiality
and the technical/cross-market state at the anchor; an outcome records only
exchange observations strictly after it, against a baseline at or before the
anchor. Outcome method `outcome-v6` filters receipts at an explicit replay `as_of`
(the service evaluation time), so delayed 5m/15m outcomes become measurable once
their samples arrive and retain the delayed-feed label. A late baseline may
measure a retrospective return but cannot populate an anchor-time snapshot.
Endpoint samples must be within 60 seconds of the measurement boundaries;
conflicting endpoint timestamps fail closed and sampled interior gaps remain
visible. Evaluation method `evaluation-v5` and current-version readers exclude
older incompatible outcomes/verdicts without rewriting stored history.
The two are separate records, so production state
never receives future data and a historical query can never be answered with
today's revised context. `measure_horizon` filters the series before any
arithmetic, and explicit tests construct misleading pre-anchor moves to prove they
cannot leak.

For live file-backed history, source callbacks and deterministic record building
stay on the API loop. A dedicated writer thread owns a separate SQLite connection
and commits each snapshot/outcome bundle atomically. The loop-owned connection
serves reads under WAL; in-memory test stores retain synchronous writes.

**Deterministic labels.** Reaction persistence (`PERSISTED` / `EXTENDED` /
`PARTIALLY_REVERSED` / `FULLY_REVERSED` / `MIXED` / `INSUFFICIENT_DATA`) is
assigned by code from documented percent thresholds. An optional LLM may explain a
cohort but never assigns the label and is never required for a query to work.

**Sample discipline.** Every aggregate carries `sample_size`, `date_range`,
`missing_count` and a readiness tier; a sample below the policy floor is reported
as insufficient rather than as a percentage. Poor-quality outcomes are excluded by
default and the exclusion count is reported. Outliers are never silently trimmed.

**Probability calibration** applies only to genuine binary `noul` probabilities
(a Jev belief). A declared analyst `confidence` is labelled as such and is never
treated as a true probability.

**Boundaries.** `core/historical` measures only: it never changes materiality
weights, Jev/routing/notification thresholds, prompts or model selection, and it
writes no policy. It reads persisted outcomes rather than recomputing market
facts, and its queries are typed and parameterized (no arbitrary SQL). The service
keeps source callbacks and writer connections on their owner thread. File-backed
HTTP read projections open and close independent read-only SQLite connections in
workers, with two concurrent readers, a one-second queue budget, and a five-second
SQL/CPU budget. Cancellation is checked during scan/validation/statistics batches;
an individual native operation can finish before the next cooperative check.
Read endpoints do not persist calibration reports, query logs or assessments.
Ephemeral in-memory test stores retain their owner-thread access.

The same bounded read projection mechanism serves recent analyst records and
materiality/Jev research scans. Workers own their read-only connections; source
callbacks, policy capture and writes remain on the event-loop owner thread.
Recent-analysis response serialization also runs off-loop. News normalization
uses pure worker batches of five items, with owner-thread persistence and a
yield between completed batches; no transaction crosses an await.

The web analyst feed renders twenty records per page and mounts detailed
interpretations only when opened. Pagination retains access to every fetched
record; evidence qualification, timestamps and provider health remain separate.
Calendar source notices are grouped by core-issued presentation identities, not
by frontend inference. Verified ISM notices may attach to the exact scheduled
publication slot when report period, country, unit and numeric values agree;
generic consensus `release` cadence is compatible with issuer `monthly` for
these canonical monthly releases. Source records remain independently readable.

---

### 4.13 Implemented in Phase 12 (calibration review + historical similarity)

```
Phase 11 point-in-time evidence (snapshots + outcomes)
        │
        ├── core/research/features.py   → versioned point-in-time vector
        │         │
        │         ▼
        │   core/research/distance.py   → weighted, coverage-aware distance
        │         │
        │         ▼
        │   core/research/similarity.py → episode-deduped ranked cases + outcomes
        │
        └── core/research/calibration_review.py → deterministic findings
                  │  (sample / effect / significance gates)
                  ▼
            ReviewRecommendation (human review only)
                  ▼
        SQLiteResearchRepository → API / WS ?topics=research → web + Qwen
```

| Concern | Location | Responsibility |
| --- | --- | --- |
| Domain | `core/domain/research.py` | `ReviewRecommendation`, `SimilarityQuery/Result/Case`, feature versions, statuses |
| Features | `core/research/features.py` | Versioned point-in-time vector; no outcome field |
| Distance | `core/research/distance.py` | Weighted, coverage-aware distance + component breakdown |
| Episodes | `core/research/episodes.py` | Stable episode identity so duplicate anchors collapse |
| Similarity | `core/research/similarity.py` | Ranking, episode dedupe, no-future guard, aggregate |
| Review | `core/research/calibration_review.py` | Materiality/Jev/drift findings and gates |
| Statistics | `core/research/statistics.py` | Seeded bootstrap difference interval, rank effect size |
| Service | `core/research/service.py` | Review scheduler, dedupe/staleness, similarity queries, bus |
| Storage | `core/storage/research.py` | Append-only versioned recommendations + review runs |
| API | `core/api/research.py` | `/research/*`, `?topics=research` |
| Configuration | `core/config/settings.py` | `research_*` flags, thresholds, scan cap |
| Presentation | `web/src/research/`, `hooks/useResearch.ts` | Similar Cases, Calibration Review, Recommendations |

**Phase 11 limitations addressed first.** All filterable outcome dimensions are
denormalized into indexed columns (`category`, `session`, `surprise_direction`,
`rates_direction`, `dollar_direction`, `volatility_state`, `vwap_position`,
`structure_trend`) and the snapshot table gains `event_class`, `importance`,
`materiality`, `nq_relevance` and `novelty`, so a selective cohort scans the
complete eligible population. A bounded cohort reports `cohort_complete = false`,
`scanned_count`, an exact `matched_count` and a `limit_reason` rather than
presenting a partial aggregate as complete. The migration is non-destructive: new
columns are added and backfilled from each row's own stored payload, and a
self-healing pass also fills any managed column left NULL by an older running
process. A manual backfill and the periodic worker are single-flight, backfill
observation reads yield between instruments, and the review generators yield
between groups, so research never starves live ingestion. The review status poll
reads a cached suppression summary that the worker refreshes, so it never runs a
scan on the request path.

**Deterministic, local, zero-LLM-cost.** Both capabilities are pure statistics
over persisted data. Similarity uses a straightforward local nearest-neighbour
scan — no vector database, FAISS or external service was introduced because the
current data size does not require one. The API path scores candidates in bounded
batches and yields to the event loop between them, and the scan cap and minimum
coverage are clamped server-side, so a research query cannot stall live
ingestion. An over-cap scan reports `scan_complete = false` with `matched_count`
and `limit_reason` rather than silently truncating.

**No lookahead.** A similarity vector is built only from
`HistoricalFeatureSnapshot` fields. `vector_from_current` has no outcome
parameter, and the current-context source exposes no outcome field, so a
current-market query cannot express future data. The engine also refuses to match
a snapshot recorded after the query time, and range bounds are normalized to UTC
so the comparison is exact. Tests prove a planted large later move does not
change a current-context distance. Jev calibration uses the decision revision
that existed at the anchor, never a post-hoc re-decision.

**Coverage and weighting.** A feature missing on either side is excluded from the
distance and lowers feature coverage; coverage is measured against the full
feature schema (not the pair's union), so two sparse snapshots cannot report a
perfect score. A minimum shared-feature count and a configurable
`minimum_feature_coverage` prevent a one-field match from scoring high. Group
weights are a versioned policy (`similarity-weights-v1`), applied per feature
within a group so a larger group cannot dominate by count.

**Episode deduplication.** An anchor that names an `event_id` belongs to that
event's episode, so the event, materiality, analyst and synthesis anchors for one
real episode contribute one nearest-neighbour example instead of several. The
same dedup is applied before any calibration-review statistic, so an event's
event anchor and materiality anchor are one observation, not correlated
pseudo-replication.

**Review gating.** A recommendation requires a minimum sample per group,
acceptable data quality, a bootstrap interval for the median difference that
excludes zero, and a practical effect threshold. A statistically detectable but
operationally trivial difference is reported as `STATISTICAL_SIGNAL`, not
`PRACTICALLY_MEANINGFUL`. Findings that fail a gate are returned with their
suppression reason so the UI shows *why* nothing was recommended. A human
decision on a recommendation is never overwritten by a later run: a finding id is
deterministic from its signature, and an existing row for that signature is left
untouched. `STALE` is engine-set only — a human cannot set it — and is applied
only when the generator ran with data and the evidence no longer qualifies, or
when the component version changed; a transient loader failure cannot flicker a
finding. The engine never changes materiality weights,
Jev/routing/notification thresholds, prompts or model selection.

**Boundaries.** `core/research` consumes the Phase 11 evidence layer rather than
creating a second historical data model; it computes statistics, it does not
measure new market facts. `core/analysis` stays pure. The web and Qwen surfaces
render results and never re-derive them; Qwen never decides similarity and never
claims a policy change.

---

## 5. Data correctness requirements

- Timestamps are stored in **UTC** and converted only for display.
- Every record preserves **source** and **provenance** (provider, source
  timestamp, retrieval timestamp).
- Data is explicitly **live**, **stale**, or **unavailable** — never conflated.
- Market sessions and holidays are respected; the system never assumes a market
  is open.
- Revisions, missing values, and out-of-order arrivals are handled explicitly.
- Analytics are reproducible from stored inputs.

---

## 6. AI analyst layer (Phase 7 — native, PydanticAI + Ollama Cloud)

Phase 7 is **not** Hermes. It is a native Omarkets service that consumes the
Phase 6 escalation queue and produces validated, evidence-grounded specialist
analysis. The specialist analysts are:

- macro analyst
- economic-release analyst
- technical analyst
- news analyst
- geopolitical analyst
- cross-market analyst
- synthesis / chief analyst

They consume structured market state produced by the core. They do not ingest
raw data, do not browse the web, have no tools, and never compute a deterministic
value.

### 6.1 PydanticAI's exact role

PydanticAI is used **lightly**, as a typed interface to a reasoning model:

```
Structured Omarkets context
        ↓
PydanticAI Agent (single shot, output_type=AnalystOutput, no tools)
        ↓
Ollama Cloud model
        ↓
Validated Pydantic object
```

It is **not** the application architecture. Omarkets retains ownership of:

- the escalation queue and job claiming,
- routing and which analysts run,
- the database, retries, lifecycle and state,
- model selection and fallback policy,
- evidence-grounding validation,
- the API, WebSockets and failure handling.

**Pydantic Graph is not used.** The workflow is a fixed fan-out/fan-in that
plain asyncio expresses clearly; graph machinery was deliberately not adopted to
avoid coupling the core to a beta API. Graph-specific code, if ever added, must
stay isolated.

### 6.2 Model policy

Models are configuration, never hard-coded, and Omarkets — not the model —
selects them:

- normal specialist analysis uses `OMARKETS_ANALYST_MODEL`,
- a bounded primary failure may retry once on `OMARKETS_ANALYST_FALLBACK_MODEL`,
- a **complex** synthesis (CRITICAL materiality or material disagreement) may use
  `OMARKETS_ANALYST_SYNTHESIS_MODEL`; it is not used by default,
- a fallback result records `used_fallback` so fallback use is never disguised.

The initial exact model IDs are configured from the operator's Ollama Cloud
account; the code defaults to the current configured equivalents
(`deepseek-v4.1-flash` primary, `glm-5.3-flash` fallback).

`OMARKETS_ANALYST_PROVIDER` explicitly selects `ollama_cloud` (default) or
`openrouter`. OpenRouter uses the fixed OpenAI-compatible endpoint
`https://openrouter.ai/api/v1` through the same typed, tool-free PydanticAI
adapter. Its separate `OPENROUTER_API_KEY` (or `OMARKETS_OPENROUTER_API_KEY`)
never supplies the TypeSafe decision layer; Jev requires `TYPESAFE_API_KEY` or
`OMARKETS_TYPESAFE_API_KEY`. The former OpenRouter aliases for the TypeSafe key
are no longer accepted. Unknown analyst providers fail configuration validation;
missing provider credentials fail closed. Model fields remain operator-configured:
select provider-qualified OpenRouter model IDs for primary, fallback, and any
synthesis override; Ollama model IDs are not translated automatically. Routing,
concurrency, evidence checks, and workflow analysis budgets apply to both providers.

### 6.3 Evidence grounding

Every supplied fact has a stable reference id (`MKT.US2Y.CHANGE_BP`,
`TECH.NQ.VWAP`, `MACRO.CPI_MOM`, `REACT.5m.NQ.PCT`, ...). Analysts cite ids.
A deterministic post-validator flags any cited id that was not supplied and any
numeric token in prose that matches no supplied value. Analysts cannot invent
prices, percentages, event values, source names or timestamps.

### 6.4 Hermes and Qwen are outside this pipeline

Hermes is **not** part of the analyst runtime. It is an optional general-purpose
coding/agent tool that the Qwen voice assistant may launch (alongside OpenCode
and Codex) via its existing `launch_agent` tool. It talks to the Omarkets API
like any other client:

```
USER → Qwen → Hermes/OpenCode/Codex → (their own work)
```

Omarkets analysis works whether Hermes is running, crashed, changed or removed.
Omarkets has no Hermes dependency.

The voice interface itself is Phase 9 and is also outside the analyst runtime:
Qwen is ears and presentation, and reaches Omarkets only through the read-only
MCP adapter described in §4.10.

Example surface (illustrative only):

```
NQ +0.42% | 2Y +4bp | DXY +0.18% | VIX 16.8 | CPI 18m
```

---

## 7. Building vs running

| Phase | Harness | Role |
| --- | --- | --- |
| **Building Omarkets** | **Codex / OpenAI** | Software-engineering harness: writes, reviews, tests, documents code. |
| **Running Omarkets** | **Omarkets core** | Deterministic engines + native Phase 7 analyst service. No Hermes or Qwen dependency. |
| **Human interface** | **Qwen voice** (Phase 9) | Ears, conversational routing and presentation over the Omarkets API. Never the intelligence pipeline. |
| **General agents** | **Hermes / OpenCode / Codex** (optional) | Coding and general agent work, launched by Qwen. Never a Omarkets dependency. |

The **Omarkets native analyst service** is the
run-time intelligence pipeline. **Qwen** (Phase 9) is the human interface; it
reads Omarkets over the read-only MCP adapter and never computes market facts.
**Hermes** is an optional general-purpose agent Qwen may launch and is never a
dependency of analysis.

### 7.1 Revised roadmap

| Phase | Scope |
| --- | --- |
| **7 — Native Analyst Layer** | PydanticAI + Ollama Cloud specialist & synthesis analysis over the escalation queue. |
| **8 — Native Omarchy Integration** ✅ | Shell bar widget, compact popup, economic-event countdown, high-materiality + analyst + release notifications, hotkeys, session startup, focus-existing launcher. |
| **9 — Qwen Voice + Omarkets Interface** ✅ | Qwen Audio realtime voice routes natural-language market questions to a read-only Omarkets MCP tool surface over the loopback API. Qwen is ears + routing + presentation; Omarkets remains the market brain. |
| **10 — Daily Briefings / Workflow Automation** ✅ | Calendar-driven pre-market/event/follow-up/session briefings, calendar reconciliation, analysis reuse with a bounded fresh-analysis budget, structured versioned briefings, notifications, and voice retrieval. |
| **11 — Historical Intelligence + Calibration** ✅ | Deterministic outcome measurement after anchors, point-in-time snapshots, rule-computed NYSE holiday/half-day calendar, materiality/Jev/analyst calibration, safe cohort queries, FOMC lifecycle measurement, and versioned reports. Measurement only; no automatic policy change. |
| **12 — Calibration Review + Historical Similarity Intelligence** ✅ | Review-gated calibration findings for human review, complete-cohort queries, a deterministic point-in-time similarity engine with episode dedupe, and read-only web/Qwen surfaces. No automatic policy change. |
| **13+ — Optional advanced research / forecasting / integrations.** | Only if justified by observed need. Optional forecasting, if ever added, must stay research-only and out-of-sample. |

Hermes is **not** a required Omarkets phase. It remains an optional
general-purpose coding/agent tool that Qwen may launch via its existing
`launch_agent` tool (alongside OpenCode and Codex); Omarkets has no Hermes
dependency and works whether Hermes is installed or not.

**Not implemented in Phase 8** (deliberately deferred): automated daily
briefings, historical calibration, automated trading, order execution, portfolio
execution, autonomous agent research, arbitrary OS automation from Omarkets.

**Not implemented in Phase 9** (deliberately deferred): voice-controlled trading
or order placement, voice editing of Omarkets thresholds or model policy,
unrestricted Omarkets admin over MCP, Qwen-generated replacement market analysis,
automatic internet research for market questions, a persistent conversational
memory system, and any modification inside the Qwen realtime audio loop.

**Not implemented in Phase 10** (deliberately deferred): analyst historical
scoring, automated threshold tuning, historical backtesting, model calibration,
automated prompt optimisation, self-learning agents, automated trading, order
execution, portfolio execution and trade recommendations. These belong to
Phase 11+ or may never be required.

**Not implemented in Phase 11** (deliberately deferred): automatic policy or
threshold rewrites, automatic materiality-weight changes, automatic model
switching based on small samples, reinforcement learning, model fine-tuning,
autonomous prompt optimisation, hidden self-modification, LLM-generated
historical statistics, hindsight leakage into live analysis, automated trading,
order execution, portfolio optimisation and trade recommendations. Phase 11
measures and reports; any future policy adjustment must be explicitly reviewed.

**Not implemented in Phase 12** (deliberately deferred): autonomous policy
changes, automatic threshold/prompt/model rewriting, reinforcement learning,
online self-training, automated trading, order execution, portfolio
optimisation, arbitrary SQL, similarity based on future outcomes, LLM-decided
similarity or LLM-generated statistics, and optional forecasting research. Phase
12 produces deterministic findings and similar cases for human review only.

---

## 8. Open questions

Recorded for future decisions, not yet resolved:

- Concrete data providers and their licensing/rate limits.
- Storage split between DuckDB and SQLite.
- How materiality scoring is defined and calibrated.
- Local charting library selection.
- Packaging and systemd user-service layout.

Resolved in Phase 1:

- **Market state transport** is push: a WebSocket snapshot stream plus REST
  polling. Both are available; the UI can choose.
- **Staleness** is a per-asset-class freshness threshold, configurable through
  `OMARKETS_STALE_OVERRIDES`. It is not yet session-aware.
- **Provider adapters** live under `core/providers`; `core/ingest` is reserved
  for the normalization/ingestion pipeline above a provider adapter.
- **Change baseline** is provider-supplied (`previous_close` in observation
  metadata), so change survives restarts and is never fabricated by the core.

Not yet modeled, and required before real data is trusted:

- Market sessions, exchange holidays, and half-days (§5). Until then `stale`
  means "older than its freshness threshold", not "market closed".
# Paid AI cost admission (October 2026)

`core/services/ai_budget.py` is a shared deterministic admission boundary for
TypeSafe Jev and LLM HTTP attempts. An additive SQLite ledger in data-dir
`ai-cost-control.sqlite3` reserves conservative model-specific maximum costs
atomically before network I/O, settles verified usage, and retains unknown
timeout/crash liability across restarts. The combined monthly limit defaults
to $5; remaining calendar-day pacing and a $0.20 maximum daily limit prevent
front-loading. Budget periods use an explicit local time zone. All primary/fallback/manual
requests share admission; critical events do not bypass it. Approved prices and
serialized input/output bounds fail closed for unknown models. API settings
never increase budgets automatically or top up provider accounts.

Persistent account breakers stop 401/402 storms; explicit recovery does not
refund usage. Durable pause notifications use the existing desktop notifier;
read-only `/api/v1/ai/budget` reports delivery failures, reserves, remaining
allowance, provider blocks and reset time. UI treats AI pause separately from
API availability and quote age. Existing current-month spend must be seeded
once at migration; separate clients on the same key are outside this boundary.

Jev semantic inference identity is separate from exact context provenance.
Quote/report refreshes reuse original judgments without duplicating usage;
unavailable semantics remain unavailable, not guessed. At most one eligible
high-materiality reaction follow-up is automatic per semantic version.
Analyst reuse/link tables preserve immutable original results across escalation
IDs. Quarantined results remain quarantined; all-quarantined specialists do not
trigger paid synthesis. Automatic analyst backlog older than six hours expires.

### October 3 read concurrency boundary

Briefing list API reads use the existing bounded read-projection helper with a
worker-owned read-only SQLite connection; workflow writes retain their original
thread-bound connection. Latest briefing identities are selected in SQL before
payload validation. Budget health/status/shell projection awaits one accounting
snapshot on a worker using the budget guard's existing cross-thread connection
and lock. Shutdown drains tracked reads (including cancelled callers) before
closing under that lock off-loop. Admission, settlement and stored charges retain
the same rules and source of truth.

Release reconciliation evaluates existing dated-evidence eligibility before
constructing market context, shares one calendar/time snapshot within a pass,
and yields between rows. Jobs evidence uses EconomicService.latest_observations()
to select the same registry-matched current-vintage observations from the owner
thread cache without rebuilding macro history and calendar state. Existing
qualified builders retain responsibility for validation and durable job identity.


### Stage B2 freshness and interval semantics (7 October 2026)

CNBC normalization declares expected source delay (WTI/GOLD 600 seconds, VXN
900 seconds), retains exchange timestamps, and labels GOLD as futures. Typed
instrument state exposes expected delay separately from exchange age and excess
age. Freshness compares excess age with the class threshold, with a minimum
60-second margin for declared expected delay; explicit stale/unavailable source
status still wins. Feed delay remains visible even when excess age is stale.
No observed lag is automatically converted into an expected delay allowance.

Cross-market and briefing evidence distinguish delayed from stale. Evidence
retains a typed feed-status field; delayed facts may be available, while actual
staleness stays strict. Relative NQ/ES and briefing direction comparisons require
matching sources, source timestamps within 60 seconds, and compatible provider
baseline quote-date periods (legacy state uses the New York quote date). The
briefing snapshot carries data status independently of feed status, and persisted
comparison eligibility defaults false for historical items.

Technical calculation version 2 stores a volatility result for every computed
intraday interval. ATR and realized volatility use completed bars in that
interval; realized volatility uses the active session. Session and opening-range
metrics continue using finest bars. The timeframe query selects both structure
and volatility; volatility discloses its interval. Legacy states default version
1 and unknown volatility interval, cannot masquerade as selected version-2
volatility, and remain readable. Technical state itself is recomputed in memory;
no technical-snapshot database migration is required. Persisted historical
analyses/briefings are not rewritten or resubmitted.

Prose-number screening excludes identifier digits and clock/window tokens and
accepts supplied deterministic delayed values. Exact typed numerical claims
still require available evidence with matching identity, unit, value, period,
direction and timestamp; heuristic screening does not certify these claims.

### Stage C2 admission and saved-attempt presentation (7 October 2026)

`GET /api/v1/ai/budget` adds typed `analysis_eligibility`: the selected analyst
provider's recorded refusal and dollar refusal, recovery kind and recheck time.
An unblocked snapshot is conditional: evidence and exact request reservations
remain authoritative at admission. This read endpoint performs no provider call.

`GET /api/v1/workflows/status` projects the latest 200 durable runs against actual
analyst job state and original result timestamps. Its linked `active_queue`
deduplicates jobs; it is not a count of every analyst job outside that run scope.
New request outcomes store the job ID separately from accounting admission IDs;
legacy attempt suffixes resolve to the original job. Saved briefing analysis IDs
remain distinct from later recorded results until qualified evidence selection
assembles a version. UI pending labels describe the historical assembly/attempt.

Unsubmitted blocked context rechecks retain their run identity across scheduler
buckets and restored baselines, at the existing five-minute cadence and bounded
evidence window. Material context changes retain separate admission identities.
The current validated output schema supplies neither scenarios nor invalidation
criteria; briefings show one explicit limitation without another model request.
Dollar guards, provider policy, retry bounds and saved history retain ownership
in their existing services.

### Bounded historical status and analyst list reads

`GET /api/v1/historical/status` is a last-valid background projection for
file-backed stores. Cold responses return `freshness: initializing` with null
counts, never invented zero coverage. A single bounded read-only worker refreshes
it; warm requests return immediately. `computed_at` dates the database projection,
`source_computed_at` dates the anchor discovery reused from the normal historical
tick/backfill, and `generated_at` dates the response. Pending remains null until
that discovery has run. A projection older than 30 seconds, invalidated by
historical publication/discovery, missing pending data, or based on discovery
older than twice the tick interval (minimum 30 seconds) is stale. Refresh attempts
are separated by at least five seconds. `refreshing` and a sanitized
`refresh_error` expose recovery while preserving the last valid values. CLI
synchronous status and ephemeral in-memory stores keep their direct read path.
Status requests stage no source reads and never move live SQLite connections to
workers. Worker-owned read connections retain cooperative SQL/CPU budgets and
reader slots until cancellation actually finishes.

`GET /api/v1/analyses/summaries` returns typed `analyses`, `total`, `limit`,
`offset`, `has_more`, and `generated_at`. Pages default to 50, cap at 100, and
allow offsets from 0 to 100000. Optional `analyst_type` and `status` filters apply
to the latest saved result per escalation and specialist, ordered by descending
saved sequence. Count and rows share a read snapshot. The SQL selects scalar
metadata, display summary, and reference strings only; it does not deserialize
full evidence, claims, or complete output records into Python. The read-only
`current_output_eligible` summary scalar excludes the retired technical analyst
and any result carrying `TECH.` evidence or claim references from current
presentation. The same eligibility gate applies to workflow reuse/assembly and
shell projections. Briefing projections set `analysis_usable=false` for retired
items (including legacy lowercase technical labels), retaining original content,
provenance and assembly purpose for historical detail. Summary/error display text caps at
2000/1000 characters; quarantined summary text remains unavailable. Exact
`GET /api/v1/analyses/{analysis_id}` retains full original output and evidence.
The existing `/analyses/recent` contract remains compatible.

The web analyst feed requests server pages of 20 summaries with analyst/status
filters. Each cached page is keyed by its complete query; a failed new page
cannot present rows retained from a different query. Last valid rows and their
server timestamps remain visible on read failure. Cards fetch the exact saved
analysis only when evidence/audit detail is opened, with cancellation and a
read-only retry. Separate offset requests can shift when new results arrive.
Historical count displays preserve null as unavailable, show projection/source
computation timestamps and backend freshness, and retain measured zero as zero.

Browser catch-up reconciles once on mount and checks authoritative status with
GET after transport uncertainty or an online/visible return. It never queues a
second POST behind an in-flight request. Request progress is separate from the
backend job state; timeouts, HTTP/network failures and invalid responses are
sanitized structured errors. GET recovery uses up to five exponential backoff
retries (2–30 seconds), pending polling is capped at 60 follow-up reads, and a
manual status retry remains available after either cap. Offline/hidden/unmounted
requests are cancelled. A browser timeout does not imply backend cancellation
or permission to repeat paid work. After a confirmed terminal status, an explicit
manual refresh may request reconciliation; backend admission/reuse remains authoritative.


### Stage F dated interpretation presentation

The briefing read projects `has_current_usable_analysis` from saved items: current
purpose, validated interpretation, available summary and known original analysis
time. `analysis_usable` is independent of evidence quality and comparison
eligibility. Qualified degraded v2 results require completed output, validated
numerical claims, no claim issues or unsupported-number/reference warnings, and
revalidation against original stored evidence. Legacy degraded items require an
exact full-result match for summary, evidence, warnings, time, context and event
revision; missing validation remains unusable. Existing checked legacy selection
is preserved. `has_current_checked_analysis` retains its original meaning. Overview fetches the newest qualifying saved version from
the latest 100 briefing summaries, validates its ID/version, and displays original
analysis times separately from market/technical/macro snapshot and assembly times.
This selection describes the saved assembly; it never asserts live freshness.
Changes and catalysts come from that same saved briefing. Conditional scenarios
and explicit invalidation criteria remain unavailable in the validated analyst
schema. Background/unverified/quarantined output stays accessible in source
and attempt disclosures. Overview polling and detail reads never trigger analysis.

The additive shell v1 projection includes analysis evidence availability/as-of,
quality, technical ATR interval/data timestamp, and the same effective admission
explanation as `/ai/budget`. Its latest interpretation selects completed usable
output within the newest 20 results using the same core validation gate. QML
consumes `interpretation_usable`, displays degraded qualifications and original
preparation time, and keeps health/source rows behind an explicit button. No QML market calculation or admission decision
is introduced; old payloads lack the required proof and are not labelled checked.
Calendar source disclosure renders core occurrence aliases and every supplied
source record's revision, schedule, receipt time and values without reconciliation
or value substitution in the browser.


Briefing analysis items optionally persist `analysis_evidence`, copied from the
exact stored AnalystResult used for that item through its AnalysisDigest. Each
entry retains typed evidence including availability and original `as_of`; a
missing legacy field remains unavailable. Original analyst source times are
presented separately from saved briefing snapshots and assembly time, which
cannot substitute for analyst evidence. Both Overview and briefing-card detail
reads reject any ID/version mismatch against the selected summary and request a
briefing refresh before displaying source detail.


Historical browser reads retain last valid counts/report and original computation
time after transient failures, with explicit last-known/error presentation. Failed
reads and cold initializing status recover with 2–30 second exponential polling,
then return to the normal 30 second cadence after success. Reader deadlines and
calibration calculations remain unchanged; unavailable data never becomes zero.

## Weekly paid AI allowance (October 8, 2026)

A positive `ai_weekly_budget_usd` supersedes monthly/day dollar pacing. The shared
SQLite guard admits full maximum liability atomically against weekly settled
spend plus all unresolved reservations, including reservations from earlier
weeks. Settlement timestamps assign actual charges to the settlement week;
Monday 00:00 in `ai_budget_timezone` starts the next week. The operator activation
CLI records an idempotent reset identity, UTC epoch, reason, and settled-charge
baseline without modifying historical charges or discarding reservations.
Authentication/credit/integrity blocks remain explicit recovery requirements.

The typed budget status adds active period, weekly spent/reserved/remaining,
reset timestamp and timezone. Web presentation consumes these facts directly;
legacy month/day limits report 0 while weekly mode is active. Explicit provider
count caps remain configurable; weekly mode removes implicit OpenRouter daily
and background counts.

### Current calendar actual coverage and schedule freshness

Calendar numeric joins distinguish `current_vintage` BLS/FRED series values from
verified current issuer `source_release` values. CPI/core CPI selects monthly or
annual series explicitly; PPI and retail joins require the exact monthly metric,
reference month, units and elapsed release slot. Core retail uses the Census
ex-auto series RSFSXMV rather than the headline RSAFS series. Issuer Census retail
and Federal Reserve G.17 adapters validate the current release and preserve source
hashes. Census retail reads the Census API (`timeseries/eits/marts`, category
`44X72`, SA, `MPCSM`, latest complete month), which has no publication time, so
`released_at` stays null; it joins only an 08:30 slot in the month after its
reference month. Unique
issuer/calendar joins retain both source records and durable occurrence aliases.

Calendar display groups distinguish monthly and annual CPI/core CPI metrics.
`CalendarResponse.stale` reports disabled economics or the absence of a healthy
schedule source; optional numeric-source failures stay in individual provider
health and missing-actual coverage notes. Unsupported BEA/Census release metrics
remain explicitly unavailable. Neither current series nor issuer reports recreate
missing historical forecasts or guarantee the original publication vintage.
