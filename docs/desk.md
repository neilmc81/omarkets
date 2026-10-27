# Omarkets desk (Today screen)

October 19, 2026. The desk replaces the old news → Jev classifier → escalation →
analyst → briefing chain as the way Omarkets explains markets. The goal is one
clear, plain-English note at the moments that matter, written for someone who
is learning how macro news, the Fed, bond yields, earnings and geopolitics move
Nasdaq 100 futures (NQ).

## What gets written, and when

| Note | When (New York time) | Id (idempotency key) |
| --- | --- | --- |
| Week ahead | Sunday 17:00 until Monday 12:00 | `primer:<monday>` |
| Pre-market | 09:00 on trading days (after the 08:30 data, 30 min before the open), until 10:30; leads with this morning's releases | `premarket:<trade date>` |
| Economic release | 26 min after every medium/high/critical release with figures: 15 min of reaction + 10-min feed delay + 1 min margin; waits up to 45 min for a missing actual | `release:<time>:<codes>` |
| Fed | FOMC decision / minutes +26 min, press conference +71 min (60-min window + feed delay) | `release:<time>:<codes>` |
| Mega-cap earnings | once results headlines appear, 30 min–4 h after the report | `earnings:<symbol>:<day>` |
| Big move | NQ moves ≥ 0.8% over about an hour; at most one per 3 hours | `move:<hour>` |
| Market now | on request ("Explain the market now"); at most 5 per day; a note from the last 15 minutes is shown again instead of writing a new one | `ask:<time>` |
| Treasury auction | 26 min after a notably strong or weak 2/5/7/10/20/30-year or TIPS auction (rule below) | `auction:<date>:<cusip>` |
| Week in review | after the week's last cash close + 15 min (Friday 16:15, or Thursday in a holiday-shortened week), for up to two days | `review:<monday>` |

All schedule times are New York clock times computed with the `America/New_York`
time zone, so they follow US daylight saving automatically (e.g. pre-market is
13:00 UTC in summer and 14:00 UTC in winter; 22:00 vs 23:00 in Tokyo).
Holidays are rule-based, so future years need no update.

Releases older than six hours are never backfilled. A failed note is retried
after five minutes, at most three times (see [Failed notes](#failed-notes)).
A successful note is never overwritten,
so a restart cannot produce a duplicate note or notification.

## Boundaries

- `core/desk/facts.py` computes every number: release grouping across sources,
  beat/miss verdicts at the official reporting precision (0.6485 stored vs 0.6
  forecast is *in line*), price and yield moves between observed quote times, the
  quote strip and the bond-market view (2-year, 10-year, 10y–2y curve, day and
  one-week changes). Missing data is omitted or shown as unavailable, never zero.
- `core/desk/followup.py` measures "did it hold?" (below); `core/desk/review.py`
  works out the week for the weekly review.
- `core/desk/writer.py` makes one OpenRouter call per note (up to three: one
  retry on `desk_fallback_model` if that fails, and one regeneration on the
  writing model if the note contains numbers not in the facts; each call is
  reserved separately) with the facts as JSON
  and the beginner-focused prompt in `core/desk/prompts.py`. Every call is
  reserved and settled through the existing `AICostGuard` (weekly cap from
  `OMARKETS_AI_WEEKLY_BUDGET_USD`). The guard sends one desktop alert per week
  when settled spend reaches 80% of the cap, and one critical "budget used up"
  alert when notes pause because the cap cannot cover another note. Output is
  validated (`NoteDraft`) and rejected if it contains trading advice.
- `core/desk/service.py` schedules notes and sends one desktop notification per
  successful note. `core/desk/store.py` keeps notes in `data/desk.sqlite3`.
- `core/providers/earnings_nasdaq.py` reads mega-cap earnings dates from the
  public Nasdaq calendar.
- API: `GET /api/v1/desk`, `GET /api/v1/desk/notes/{id}`, `GET /api/v1/desk/learn`,
  `GET /api/v1/desk/playbook`, `GET /api/v1/desk/calendar`, `GET /api/v1/desk/macro`,
  `GET /api/v1/desk/news`,
  `POST /api/v1/desk/ask`.
- UI: `web/src/today/` is the default screen, one of seven pages (Today, Chart,
  Calendar, Macro, News, Playbook, Learn). The
  old **Details** workstation is gone: `?view=details` and `?section=overview`
  open Chart, `?section=macro|calendar|news` open those pages, and retired
  deep links (`analysts`, `briefings`, `history`, `technical`) open Today. Core
  and source diagnostics are in the status panel behind the feeds dot. The
  Omarchy popup shows the latest note's headline and summary.

## Configuration

| Variable | Default |
| --- | --- |
| `OMARKETS_DESK_ENABLED` | `true` |
| `OMARKETS_DESK_MODELS` | `{}`; JSON map of note kind to model, e.g. `{"move": "..."}` |
| `OMARKETS_DESK_MODEL` | unset; the old single model, used for every kind not in `DESK_MODELS` |
| `OMARKETS_DESK_FALLBACK_MODEL` | `anthropic/claude-opus-5.5`; empty disables the retry |
| `OMARKETS_DESK_MODEL_PARAMS` | `{}`; JSON map of model to extra request fields, replacing the default per model |
| `OMARKETS_DESK_MODEL_RATES` | `{}`; JSON map of model to `[input, output]` USD per million tokens |
| `OMARKETS_DESK_INPUT_RATE` / `_OUTPUT_RATE` | `3.0` / `15.0`, for models with no rates (reservation upper bound) |
| `OMARKETS_DESK_MOVE_THRESHOLD_PCT` | `0.8` |
| `OMARKETS_DESK_NOTIFY` | `true` |
| `OMARKETS_DESK_DAILY_PLAN` | `true` (the 07:00 ET plan notification) |
| `OMARKETS_DESK_LOCAL_TIMEZONE` | `America/New_York` (times in the daily plan) |

### Which model writes a note

The first match wins: `OMARKETS_DESK_MODELS[kind]`, then `OMARKETS_DESK_MODEL`,
then the built-in defaults. By default `review` and `premarket` notes go to
`anthropic/claude-opus-5.5`; every other kind goes to
`deepseek/deepseek-v4.1-flash-1017`. Both go through OpenRouter. DeepSeek
requests are pinned to `deepinfra/nebius/novita/parasail` at fp8 or better
with `allow_fallbacks: false`, because the OpenRouter privacy setting blocks
DeepSeek's own provider. The DeepSeek rates are `0.15` / `0.60` and the Opus
rates are `5` / `25`.

If the first model fails (HTTP or network error, invalid output, or the advice
filter), the note is written once more on the fallback model. That retry
reserves its own budget, so the weekly cap can stop it. The note records the
model that actually wrote it. Cost is OpenRouter's `usage.cost` when it is
reported; otherwise it is worked out from that model's rates.

**Number check.** Every number in the note must appear in the prompt, either
exactly or rounded to fewer decimals. If a number does not, the note is
regenerated once. If invented numbers remain, the note is saved with them
listed in `unverified_numbers`.

## Did it hold? (release, Fed and earnings notes)

No AI call. After a note is written, the desk measures NQ, ES and the 2- and
10-year yields from the event time to two checkpoints:

- **after 1 hour** — skipped when that moment is in the 17:00 ET maintenance
  break or the market is shut, and for the Fed press conference (its note
  already measures 60 minutes);
- **by the close** — the next US cash close after the event (16:00 ET, 13:00 on
  early-close days; after-close earnings use the next trading day).

A checkpoint is measured only when the delayed feed has caught up (checkpoint +
11 minutes) and only from quotes at most 5 minutes before it; if NQ is still
missing two hours later it is recorded as "not available". The card shows one
line, e.g. *First 15 min: NQ −0.40% · after 1 hour: −0.15% · by the close:
+0.60% — the first reaction reversed.* The verdict compares NQ's later change
(from the event) with its first reaction: a first move under 0.10% is "little
first reaction"; same direction and at least 1.5× is "grew", at least 0.5× is
"held"; the other direction by 0.10% or more is "reversed"; otherwise "faded".
Until the close is measured the wording says "so far". Results are stored next
to the note (`desk_notes.followup`); the written note itself is never changed.

## Surprise sizing

For every figure with an actual and a forecast, the surprise is actual minus
forecast at the official reporting precision (the same rounding as the verdict).
It is compared with that figure's own surprises at its last 12 releases (matched
by name, so CPI m/m and CPI y/y are separate; earlier releases only). With
fewer than 5 earlier releases the label says "Not enough history yet". Otherwise
it is **bigger than usual** when larger than at least 80% of them, **smaller than
usual** when larger than at most 30%, and **typical** in between, e.g. "Bigger
than usual surprise above the forecast — larger than 9 of the last 10". The
label goes to the writer (`surprise_size`) and into the figures table. Consensus
forecasts have only been stored since mid-September 2026, so most indicators
need a few more months before they can be sized.

## Revisions (October 27, 2026)

When a release also revises the previous period's figure, release and
pre-market notes get the previous value as first reported by the source that
carries the revision (`previous`), the revised value (`previous_revised_to`) and the size and direction, e.g.
"revised down by 2K" (`revision`). Both values are compared at the official
reporting precision; a revision too small to show at that precision counts as no
revision. With no revision the fields are left out (never zero). If only the
revised value is known, `previous_revised_to` is sent without a size. Sources
that report revisions today: ADP (monthly and weekly) and DOL jobless claims;
BLS PPI rows compare the calendar's previous with the current BLS vintage, so
there the original issuer figure is not verified.
Prompt `desk:v5` tells the writer to mention a revision when it changes the
picture.

## Price location (October 27, 2026)

Pre-market, release, Fed, earnings, move and on-demand notes get
`nq_price_location`: NQ's current price against the key levels from
`core/analysis/levels.py` (the same ones on the chart): prior settle, overnight
high/low, prior day high/low and prior week high/low. Each line has the level,
its value, where NQ is (e.g. "45.00 points below" or "at") and the ET window it
was measured over, so "prior day" and "prior week" stay clear around weekends
and holidays. `overnight_so_far` is set before the cash open, and also after it
while the latest (delayed) NQ quote is still from before 09:30 ET. If the latest
NQ quote is from before the current session opened (just after the 18:00 ET
reopen) the block is left out rather than compare it with the new session. Levels that cannot be
measured (no quotes, contract roll, no settle) are left out, and with no NQ
price or no level the block is left out. The gap is not repeated: today's change
from the settle is already in `markets_today`. Prompt `desk:v6` asks the writer
to state levels as plain facts, never as support, resistance or a breakout.
The primer, week in review and auction notes do not get it.

## Week in review

One AI call after the week's last close. Facts: each market's change from the
previous week's last close to this week's (NQ, ES, 2y, 10y, dollar, VIX, oil,
gold), NQ's close-to-close change each day, the week's release/Fed/earnings/move
notes ranked by NQ's move (by the close when measured, otherwise the first
reaction) with their "did it hold?" lines and surprise sizes, the bond-market
view, and next week's schedule.

## Treasury auctions

`core/providers/treasury_auctions.py` reads TreasuryDirect's public web service:
`/TA_WS/securities/upcoming` (schedule) and
`/TA_WS/securities/auctioned?type=Note|Bond|TIPS&days=400` (results). Every field
is validated; bills, FRNs, cash-management bills, 3-year notes and malformed rows
are dropped. Reopenings count as their original term. The schedule is refreshed
every 6 hours; on an auction afternoon results are checked every 2 minutes until
they arrive (normally just after 1:00 PM ET).

- **Coming up** lists each auction with its size and a one-line explainer. When
  TreasuryDirect has not announced the time yet, it says so ("usually 1:00 PM ET").
  The 10- and 30-year are high importance, the rest medium.
- **Strong or weak (the rule).** A free when-issued yield is not available, so
  the auction "tail" cannot be measured. Instead the bid-to-cover ratio (dollars
  bid per dollar sold) is compared with the average of the previous 6 auctions of
  the same series (same original term; TIPS separate). **Strong**: at least 5%
  above that average. **Weak**: at least 5% below. Fewer than 4 earlier auctions:
  no verdict. Only strong or weak auctions get a note (one AI call, 26 minutes
  after the results, like a release); the primary dealers' share is passed as
  extra context but is not part of the rule.

## Fed speakers

The official Fed calendar (`federalreserve.gov/json/calendar.json`, already read
by the `fed_calendar` provider) lists Board members' speeches, discussions and
testimony. They are stored as schedule-only events (`FED_SPEECH`,
`FED_TESTIMONY`; the Chair is high importance, others medium) and shown in
**Coming up** with the venue. They never trigger a release note. Regional Fed
bank presidents are not in that calendar, so they are not covered.

## Breaking-news alerts

`core/desk/breaking.py`, no AI. Each desk tick checks headlines published in the
last 90 minutes from trusted sources (CNBC, BBC, the Fed, Treasury, White House,
Federal Register, State Department; not general world feeds or OFAC's routine
list). A headline alerts when it matches a rule that pairs a topic with an
action: an emergency/unscheduled Fed move, new tariffs being imposed or raised,
new sanctions on China/Russia/Iran, an attack or invasion involving the US,
China/Taiwan, Iran or Israel, a debt default or government shutdown starting, or
a trading halt. Explainers and questions ("Why…", "…?") and, for conflicts,
speculation ("possible", "warns", "prepares") never alert.

Quiet by design: one alert per headline (stored in `desk_alerts`), a near-duplicate
of an alert from the last 12 hours or the same kind of story within 2 hours is
suppressed, at most one alert per 30 minutes and four per 24 hours. Against the
stored headlines of Sep 20 – Oct 20, 2026 the rules fire zero times. An alert
sends a desktop notification, shows at the top of Today for 12 hours, and is
passed to every note written in the next 6 hours (`breaking_alerts`).

## Learn library

**Learn** in the Today header lists every note's Learn box, newest first, with
repeats merged (same text ignoring case, spacing and punctuation) and a search
box. `?view=learn` opens it directly.

## Playbook (October 23, 2026)

`core/desk/playbook.py`, no AI. **Playbook** in the Today header (or
`?view=playbook`) shows, for each indicator, how NQ moved after its releases,
grouped by whether the figure came in above, below or in line with the forecast:
e.g. *Below forecast 2 times. First 15 min: NQ fell 1, barely moved 1. By the
close: rose 2.* Expanding an indicator lists every release with the actual vs
forecast, NQ and the 2-year yield after 15 minutes, and NQ by the close with
the "did it hold?" word.

- Recomputed on each request from the stored calendar (last 365 days, read in
  90-day chunks because of the calendar's row limit) and stored quotes; nothing
  is saved. Quotes are only stored since late September 2026, so earlier
  releases are left out.
- Indicators are matched by figure name, as in surprise sizing. Figures released
  at the same moment share one market reaction; each row says how many other
  figures came out with it.
- Moves use the same rules as the notes: from the release time, end quotes at
  most 5 minutes old, NQ changes under 0.10% count as "barely moved", and the
  close is the next cash close after the first hour, measured once the delayed
  feed has it.
- Fewer than 5 releases: the indicator says it is too few to call a pattern.

## What to watch (October 23, 2026)

`core/desk/watch.py`, no AI. From two hours before every high- or
critical-importance release (not Fed speeches) until its note is written, at most
50 minutes after the release, Today shows a card at the top of the notes:

- the release's explainer, and for each figure the forecast, the previous value
  and, once out, the actual with its above/below/in-line verdict;
- the playbook's lines for that figure ("Below forecast 2 times. First 15 min: …"),
  with "examples, not a pattern" under 5 releases;
- the rule of thumb for each kind of figure in the release: inflation, jobless,
  growth and jobs, or the Fed (hawkish/dovish). These are fixed texts in
  `watch.py`, matched by event code (`CPI_MOM` uses the `CPI` rule).

The cards come with `GET /api/v1/desk` (`watch`). The playbook behind them is
recounted at most every 30 minutes and only when a card is due.

## Daily plan (October 23, 2026)

`core/desk/plan.py`, no AI. At 07:00 ET on trading days (sent until 08:25 if
Omarkets was down at 07:00) one desktop notification lists everything in Coming
up until 17:00 ET, in the reader's time zone (`OMARKETS_DESK_LOCAL_TIMEZONE`),
with ★ for high importance and the pre-market note time, e.g. in Tokyo
`21:30  Jobless claims ★`. On a quiet day it says so. One per day
(`desk_once` table in `desk.sqlite3`).

## Price storage (October 20, 2026)

Quotes are saved in `data/omarkets-live.duckdb` (table `observations`).

- **Writing.** Each batch is one `INSERT` (one commit) whose rows are sent as a
  single JSON parameter. The old row-by-row `executemany` committed every row
  (~1.5 s per 17-row batch). The October 20 multi-row `VALUES` insert took
  ~25 ms alone but 0.2–0.9 s while another thread ran Python code (binding 119
  Python parameters re-takes the GIL); one parameter takes ~5 ms alone and ~20 ms
  under load (October 21).
- **Slow-save warnings (October 21).** The 40–60 `market_batch_write` warnings
  an hour had two causes, neither a slow insert. (1) The event loop froze
  (median 0.8 s, 112 times in 11 h) mostly while news storage committed to
  `events.sqlite3`; the price write had finished but waited to resume. The news
  and economics SQLite stores now use `synchronous=NORMAL` (WAL: no disk sync per
  commit; a power cut can lose the last commits, never corrupt them), and the
  freezes stopped. (2) DuckDB syncs its log to disk on every commit, and on this
  machine (btrfs on LUKS) that sync often takes 0.2–1 s (strace: `fsync`
  0.15–0.57 s; turning off copy-on-write did not help). It runs on a worker
  thread, so the app keeps working; a quote reaches the screen up to ~1 s later.
  The warning now splits the time into `queue_ms` (waiting for a worker
  thread), `store_ms` (lock, insert and disk sync) and `resume_ms` (waiting for
  the event loop). It warns when Omarkets itself holds a batch up ≥ 250 ms, or
  when `store_ms` ≥ 4 s; shorter disk syncs are logged at DEBUG.
  `market_batch duration` now counts only the time outside the write.
- **Disk-sync stalls (October 22).** A few times an hour the disk sync stalls
  for 2–3.6 s (31 warnings at the 2 s limit in 18 h, most overnight). A bare
  4 KB write + `fsync` on this disk, with no DuckDB involved, stalls the same
  way (up to 2.2 s), so the limit is now 4 s. Retention thinning is not the
  cause: a pass holds the write lock for 1.5–3.5 s four times a day, and saves
  on the same connection afterwards were as fast as before (p50 ~15 ms).
- **Duplicates.** A re-polled quote identical to the last one saved for that
  symbol (same exchange time, value, unit, source, previous close, feed status
  and contract) is not saved again; the first receipt is kept.
- **Reading.** Reads run on their own DuckDB cursor and never wait for a write.
  `/api/v1/desk` went from 1.7–2.3 s to about 0.1 s. Startup finds the latest
  quote per symbol in the last three days first (0.2 s instead of ~5 s).
- **Retention.** Every quote is kept for 30 days. Older quotes are thinned to the
  last quote per symbol per minute, every 6 hours (`MarketStateService`
  maintenance; incremental, cutoffs on whole UTC minutes). The desk reads at
  most 10 days back, so it always sees full detail.
- **No index.** An index on `(symbol, ts)` was measured: no faster for these
  queries, so none was added.
- **Migration (schema v2).** Opening a v1 database removes consecutive repeated
  polls once (receipt order, so a value that changes and changes back is kept).
  The live database went from 4.02 M rows / 499 MB to 1.74 M rows / 186 MB; the
  untouched original is in `data/backups/omarkets-live-20261020-before-compaction.duckdb`.

## Measuring moves over long windows (October 23, 2026)

`measure_moves` used to read up to 2,000 quotes between the start and the end,
but the store returns the *newest* rows. From a release to the close NQ has
more quotes than that, so the starting quote was cut off and every "by the
close" follow-up said "not available". It now asks for exactly two quotes (the
last at the start, the last by the end).

## Failed notes

The writer allows up to 4,000 output tokens (2,500 before October 21: a model
that reasons first used up the ceiling and the October 20 premarket note was cut
off). A cut-off reply logs `finish_reason`, `completion_tokens` and
`reasoning_tokens`. A note is retried after five minutes, three attempts in all. When the third
attempt fails, the desk sends one desktop notification ("Omarkets could not
write: …" with the last error). The alert is recorded in `desk.sqlite3`
(`failure_alerted`), so a restart never repeats it.

## What was removed (October 20, 2026)

The old chain and everything that only served it is gone from the code: the
TypeSafe Jev decision engine and escalation queue, the PydanticAI/Ollama analyst
service and its prompts, briefings and workflows, catch-up analysis, release
snapshots and material-context delivery, the Phase 11/12 historical-outcome and
calibration/similarity research services, the retired technical-analysis code,
and the Details pages for analysts, briefings and history. Their API routes now
return 404 and their `OMARKETS_*_ENABLED` settings are ignored.

Kept: market data, the economic calendar and macro series, news ingestion (the
desk reads its headlines), the AI cost guard, desktop notifications, the price
history API, the shell status/popup endpoints and the theme integration.

- The shell contract is now version 2: `/api/v1/shell/status` no longer has
  `technical`, `latest_material`, `latest_analysis` or `analysis_eligibility`,
  and `/api/v1/shell/context` no longer has `recent_analyses`. The diagnostics
  list reports `desk` instead of `jev` and `analysts`.
- `/api/v1/ai/budget` no longer includes `analysis_eligibility`; the cost guard
  only knows the `openrouter` provider.
- The voice assistant's MCP tools keep their names. `market_latest_analysis`,
  `market_event_analysis`, `market_latest_briefing` and `market_brief` read desk
  notes; `market_usage` reads the weekly budget; the five history/calibration
  tools answer that the feature was retired.
- The old SQLite stores under `data/` (decisions, analyses, briefings,
  historical, research, notifications) were left on disk, untouched.

## Theme

The web app and the Omarchy popup follow the active Omarchy theme, not just
light/dark.

- The core reads the authoritative palette,
  `~/.local/state/omarchy/current/theme/colors.toml` (override with
  `OMARKETS_OMARCHY_THEME_DIR`), in `core/theme/omarchy.py`. It is served at
  `GET /api/v1/theme` and as server-sent events at `GET /api/v1/theme/stream`.
  Missing roles fall back one by one; old themes with only `color0`–`color15`
  are mapped the way Omarchy maps them.
- `omarchy theme set` swaps the theme directory and then runs
  `~/.config/omarchy/hooks/theme-set.d/`. The hook
  `omarchy/hooks/theme-set-omarkets` (install with
  `omarchy hook install theme-set omarchy/hooks/theme-set-omarkets`) calls
  `POST /api/v1/theme/reload`, so open pages retint at once. Without the hook,
  the core still notices the change within about a second.
- Web: `web/src/theme/omarchy.ts` writes the palette onto `<html>` as `--os-*`
  variables. Every colour in `style.css` and `today.css` derives from those
  through the tokens at the top of `style.css`; the chart reads the same palette.
- Popup: Omarchy retints `Color.*` itself. The state colours (up, down, warn)
  come from the palette in the collector payload, which is refreshed as soon as
  `Color` changes.
- Without Omarchy (no file, or an unreadable one) the API returns
  `source: "fallback"` and the stylesheet's built-in palette applies. That
  palette is dark, or light when the browser prefers light.
