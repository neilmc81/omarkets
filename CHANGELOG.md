# Changelog

## Unreleased

- **Retail sales, durable goods and new home sales keep working while Census
  is blocked.** Census's API has answered every request with a Cloudflare
  block (HTTP 403 or an HTML page) since Nov 2. When that happens, or no
  Census key is set, the numbers now come from FRED's ALFRED copy of the
  first published (advance) figures, so they match the headline Census put
  out and do not change when it is revised later (September durable goods:
  1.8%, not the revised 1.9%). The health line keeps the reason, e.g.
  "Census blocked (HTTP 403); using FRED fallback", and each value links to
  its ALFRED page. A month FRED has not posted yet counts as pending until
  two hours after its scheduled release (FRED's release calendar; 08:30 ET
  for retail and durables, 10:00 ET for new home sales), then as an issue.
  A month that is already overdue stays an issue when the calendar month
  changes.

- **The Sponsor button is at the top of the README.** A GitHub Sponsors
  badge sits under the opening description, linking to
  [github.com/sponsors/neilmc81](https://github.com/sponsors/neilmc81). The
  Support section still follows Costs.

- **GitHub Sponsors.** The repo now has a Sponsor button
  (`.github/FUNDING.yml`) and a Support section in the README linking to
  [github.com/sponsors/neilmc81](https://github.com/sponsors/neilmc81).

- **The Omarkets widget explains its tile colours.** A one-line legend under
  the rows reads "Colour = effect on NQ: ▲ helps · ▼ weighs on · – little
  effect"; hovering it adds that grey means little or no effect on NQ, not a
  broken tile.
- **The widget no longer says "delayed".** The per-row tags, the footer note
  and the bar tooltip's "feed DELAYED" are gone; the Today page under the asset
  strip already shows delayed vs live. DEMO, stale, closed and unavailable are
  still shown.
- **The header button is now "Desk Note (E)"** (was "Explain the market
  now (E)"). Same note, same E shortcut.
- **An ISM report that is not out yet no longer shows as a health issue.**
  Before its release (Manufacturing on the first business day of the month,
  Services on the third, both 10:00 ET, on ISM's own calendar, which skips
  the first trading day of January) a missing report is just pending. It
  counts as an issue only if it is still not listed an hour after that time.
  Fetch or parse failures are still issues straight away.
- **ADP no longer false-alarms between releases.** The old rule wanted a
  release in the last 35 days, which had no slack on five-week gaps (Sep 30
  to Nov 4, Dec 2 to Jan 6). Now each report has an expected release: monthly
  on the Wednesday of jobs-report week (Thursday when Monday to Wednesday of
  that week has a market holiday), weekly on Tuesdays (Wednesday after a
  Monday holiday, none in the monthly week, and none on Tue Dec 29, 2026, when
  New Year pushes the jobs report back a week), both 08:15 ET. A missing report
  is pending until an hour after that time, then an issue.
- **A release file dated a moment in the future is "not out yet", not a
  broken feed.** DOL jobless claims (seen at 08:29:59 ET on Oct 29), Fed G.17
  industrial production, NAR existing-home sales and Conference Board
  confidence stay healthy and simply wait for the release time (a listing
  holding only a not-yet-due Conference Board card is pending, not missing). A date more
  than a day ahead is still an error.
- **A BLS API miss right at 08:30 or 10:00 ET is retried before it counts.**
  Near those times on weekdays, a failed BLS check, or one with missing or
  unfilled series, is retried up to four times, a minute apart, instead of
  putting a 15-minute issue on the chip.

- **VIX (the CBOE Volatility Index) now gets desk notes by default.** The
  default `desk_instruments` is
  `["NQ", "ES", "YM", "GOLD", "WTI", "DXY", "US2Y", "US10Y", "VIX"]` on
  `desk:v9`. VIX works like the dollar and the yields: its own move notes
  (with its own 3-hour limit and move trigger) and "Desk Note"
  notes when VIX is the focus. There is no VIX pre-market; only NQ gets the
  daily briefing (cost policy). VIX notes say they are about VIX, and their
  tone reads "… for VIX": supportive means pushing VIX up (bigger expected
  swings, usually more worry), negative pushing it down (calmer markets);
  neither is advice, and a higher VIX is never called good news. The prompt
  explains what VIX is (the cash index on the Today strip, worked out from
  S&P 500 options, not a VIX futures price; its level is in index points) and
  what usually moves it, as tendencies rather than rules: it usually moves
  opposite to stocks and jumps most in a fast selloff, can stay firm before a
  big scheduled event and ease after it, can rise on rate, Fed or inflation
  shocks when they knock stocks lower, and has tended to fall back after a
  spike (never predicted). VIX is itself the stock market's fear gauge, so its
  notes never treat "the VIX" as an outside input and have no other gauge (no
  VXN, VVIX or MOVE index is supplied, and yields are never used as one).
  Yields are not a main driver of VIX, so its notes have no separate rates
  section. No NQ vs ES comparison or megacap count. VIX is calculated only
  03:15–16:15 ET (to 13:15 ET on an early-close day), so its typical daily
  range is measured over those hours (before, 13 hours of quotes in a 23-hour
  session never counted as a full day) and its ranges keep two decimals
  ("1.04 points (6.11%)"). A typical VIX hour moves about 0.6%, but a busy one
  4% or more, so a VIX move note needs a one-hour move of 0.55 × its typical
  daily range, between 4% and 10% (5% until there is enough history, set by
  `OMARKETS_DESK_MOVE_THRESHOLD_VOL_PCT`; `OMARKETS_DESK_MOVE_THRESHOLD_PCT`
  never applies to VIX). A VIX move note is written only while Cboe
  calculates VIX or up to 10 minutes after its close (16:15 ET, or 13:15 ET
  on an early-close day), and only from a VIX quote at most 10 minutes old, so
  an "in the last hour" note never arrives long after the close. NQ, ES, YM, gold,
  oil, dollar and yield notes are unchanged. That means a few more notes and a
  little more AI spend each week. To leave VIX out, set
  `OMARKETS_DESK_INSTRUMENTS=["NQ", "ES", "YM", "GOLD", "WTI", "DXY", "US2Y", "US10Y"]`
  in `.env` and restart; listing VIX with `desk:v8` refuses to start. After
  updating, the log line reads
  `desk notes for NQ, ES, YM, GOLD, WTI, DXY, US2Y, US10Y, VIX on desk:v9`.

- **The US 2-year and 10-year Treasury yields (US2Y, US10Y) now get desk
  notes by default.** The default `desk_instruments` is
  `["NQ", "ES", "YM", "GOLD", "WTI", "DXY", "US2Y", "US10Y"]` on `desk:v9`.
  Each yield works like the dollar: its own move notes (with its own 3-hour
  limit and move trigger) and "Desk Note" notes when it is the
  focus. There is no yield pre-market; only NQ gets the daily briefing (cost
  policy). Yield notes say which yield they are about, and their tone reads
  "… for US2Y" or "… for US10Y": supportive means pushing the yield up (rates
  higher), negative pushing it down; neither is advice. The prompts explain
  what each is (a constant-maturity Treasury yield, not a futures price, moving
  in basis points) and what usually moves it, as tendencies rather than rules:
  the 2-year follows what markets expect from the Fed most closely; the
  10-year carries more of the long-run growth and inflation outlook and the
  term premium (deficits, heavy Treasury supply, weak auctions). Because the
  note is about a yield, it reads the other yield and the curve (10-year minus
  2-year) instead of treating "yields" as an outside driver. No NQ vs ES
  comparison, megacap count or fear gauge (the feeds have no Treasury gauge;
  the VIX is never used as one). Yield notes give their ranges, distance to
  each level and move trigger in basis points ("7.2bp"), not index points. A
  yield move note needs a one-hour move of 0.55 × its typical daily range,
  between 4bp and 10bp (6bp until there is enough history, set by
  `OMARKETS_DESK_MOVE_THRESHOLD_BP`; `OMARKETS_DESK_MOVE_THRESHOLD_PCT` never
  applies to a yield), so routine 2–3bp hours don't trigger one. Their
  prompts call a yield's levels yield levels (`us2y_location`, `us2y_yield`),
  not prices, and never say yield quotes are 10 minutes delayed. NQ, ES, YM, gold, oil and dollar notes are
  unchanged. That means a few more notes and a little more AI spend each week.
  To leave the yields out, set
  `OMARKETS_DESK_INSTRUMENTS=["NQ", "ES", "YM", "GOLD", "WTI", "DXY"]` in `.env`
  and restart; listing either yield with `desk:v8` refuses to start. After
  updating, the log line reads
  `desk notes for NQ, ES, YM, GOLD, WTI, DXY, US2Y, US10Y on desk:v9`.

- **DeepSeek desk calls reserve a wider safety margin.** Before each desk
  note the AI cost guard sets aside the most the call could cost. For
  DeepSeek models that is now 2× the estimate (was 1.2×), because one live
  DeepSeek note cost more than was set aside ($0.004512 against $0.003914),
  and the guard then pauses all AI until someone checks it. Opus and every
  other model keep the 1.2× margin. Rates, the weekly budget and the
  evaluation ceiling are unchanged; a DeepSeek note still costs the same,
  only a little more budget is held while it runs and is released when it
  settles.

- **The US Dollar Index (DXY) now gets desk notes by default.** The default
  `desk_instruments` is `["NQ", "ES", "YM", "GOLD", "WTI", "DXY"]` on
  `desk:v9`. DXY works like gold: its own move notes (with its own 3-hour
  limit and move trigger) and "Desk Note" notes when DXY is the
  focus. There is no DXY pre-market; only NQ gets the daily briefing (cost
  policy). Dollar notes say they are about the US Dollar Index, and their tone
  reads "… for DXY": supportive means pushing the dollar index up, negative
  pushing it down. The prompt explains what DXY is (the dollar against six
  major currencies, mostly the euro) and what usually moves it (US interest
  rates and the Fed compared with other countries, US growth compared with the
  rest of the world, demand for safety, and inflation surprises that change
  the Fed outlook), says these are tendencies rather than rules, and gives no
  trading advice. Yields matter for the dollar, so its notes explain them; the
  feeds carry US yields only, so notes never claim the gap with other
  countries' rates. Dollar notes get no NQ vs ES comparison, megacap count or
  fear gauge (the feeds have no currency gauge; the VIX is never used as one).
  A normal dollar day moves less than one index point, so dollar notes give
  their price, the distance to each level and the day's range to three
  decimals (for example "0.420 points (0.42%)", not "0 points"), and describe
  the overnight and prior-day levels by the index's own 18:00–17:00 ET session
  rather than a futures session.
  NQ, ES, YM, gold and oil notes are unchanged. That means a few more notes
  and a little more AI spend each week, though a one-hour move big enough to
  trigger a dollar note is rare. To leave DXY out, set
  `OMARKETS_DESK_INSTRUMENTS=["NQ", "ES", "YM", "GOLD", "WTI"]` in `.env` and
  restart; listing DXY with `desk:v8` refuses to start. After updating, the
  log line reads `desk notes for NQ, ES, YM, GOLD, WTI, DXY on desk:v9`.
- **Crude oil (WTI) now gets desk notes by default.** The default
  `desk_instruments` is `["NQ", "ES", "YM", "GOLD", "WTI"]` on `desk:v9`. Oil
  works like gold: its own move notes (with its own 3-hour limit and move
  trigger) and "Desk Note" notes when WTI is the focus. There is
  no oil pre-market; only NQ gets the daily briefing (cost policy). Oil notes
  say they are about oil / WTI, and their tone reads "… for WTI": supportive
  means pushing oil's price up, negative pushing it down. The prompt explains
  what usually moves oil (supply such as OPEC+ and US production;
  geopolitics such as wars in producer regions, sanctions and threats to
  shipping routes like the Strait of Hormuz, only when the headlines show it,
  never an invented conflict; demand such as growth and China; the US
  dollar; appetite for risk; and the weekly inventory reports when the facts
  carry them), says these are tendencies rather than rules, and gives no
  trading advice. Bond yields are not treated as a main driver of oil, so oil
  notes don't need a rates section and are never asked to comment on yields;
  a data surprise is read through fuel demand and the dollar, not rate cuts.
  Oil notes
  get no NQ vs ES comparison, megacap count or fear gauge (the feeds have no
  oil gauge; the VIX is never used as one). NQ, ES, YM and gold notes are
  unchanged. That means a few more notes and a little more AI spend each week.
  To leave oil out, set `OMARKETS_DESK_INSTRUMENTS=["NQ", "ES", "YM", "GOLD"]`
  in `.env` and restart; listing WTI with `desk:v8` refuses to start. DXY
  joins the defaults too (above).
- **Gold now gets desk notes by default.** The default `desk_instruments` is
  `["NQ", "ES", "YM", "GOLD"]` on `desk:v9`. Gold works like YM: its own move
  notes (with its own 3-hour limit and move trigger) and "Desk Note"
  notes when GOLD is the focus. There is no gold pre-market; only NQ gets
  the daily briefing (cost policy). Gold notes say they are about gold, and
  their tone reads "… for GOLD": supportive means pushing gold's price up,
  negative pushing it down. The prompt explains what usually moves gold
  (yields and Fed rate expectations, the US dollar, demand for safety), says
  these are tendencies rather than rules, and gives no trading advice. Gold
  notes get no NQ vs ES comparison, megacap count or fear gauge (the feeds
  have no gold gauge; the VIX is never used as one). NQ, ES and YM notes are
  unchanged. That means a few more notes and a little more AI spend each week.
  To leave gold out, set `OMARKETS_DESK_INSTRUMENTS=["NQ", "ES", "YM"]` in
  `.env` and restart; listing GOLD with `desk:v8` refuses to start. Oil and
  DXY join the defaults too (above).
- **YM (Dow futures) now gets desk notes by default.** The default
  `desk_instruments` is `["NQ", "ES", "YM"]` on `desk:v9`. YM works like ES:
  its own move notes (with its own 3-hour limit and move trigger) and
  "Desk Note" notes when YM is the focus. There is no YM
  pre-market; only NQ gets the daily briefing (cost policy). YM notes say they
  are about YM / the Dow, and their tone reads "… for YM". That means a few
  more notes and a little more AI spend each week. To leave YM out, set
  `OMARKETS_DESK_INSTRUMENTS=["NQ", "ES"]` in `.env` and restart; listing YM
  with `desk:v8` still refuses to start. Gold joins the defaults too (above).
- **The bar popup has a new layout.** The focus instrument (NQ by default) sits
  at the top with its price, change, a 24-hour sparkline and last week's
  range. Below it are the desk note's headline, the other instruments grouped
  as Equities, Rates and Dollar · Volatility (each with a sparkline), the next
  event on one line, and a short footer. Change pills are coloured by what the
  move means for NQ, not by whether it went up or down, so a falling yield
  shows green. "Closed" now appears once in the header instead of on every row,
  and the sources/diagnostics section is gone from the popup (it stays in the
  web app's status panel). Demo quotes are dimmed, tagged DEMO and never
  coloured as helping or hurting NQ, and the footer warns "DEMO data, not real
  prices". The widget does not label delayed quotes; the Today page shows
  delayed vs live under the asset strip. After updating, run
  `omarchy-restart-shell` to see it.
- The bar popup's data now includes a 24-hour sparkline for each row, last
  week's high and low for the focus instrument, and for each quote a short
  name and whether its move helps or hurts NQ (worked out by the core from the
  risk-tone rules). The popup itself looks the same until its redesign lands.
- **Upgrading changes the desk without asking.** If your install never set
  `OMARKETS_DESK_PROMPT_VERSION` or `OMARKETS_DESK_INSTRUMENTS` (it ran on the
  old code defaults, `desk:v8` and NQ only), after updating it writes new
  notes with `desk:v9`, and ES gets its own move and ask notes. That means
  more notes and a little more AI spend each week. Older notes are kept and
  still show. What to check after updating:
  - `omarketsctl config` shows `desk_prompt_version` and `desk_instruments`
    and where each came from (`default`, `.env` or the service environment).
  - `systemctl --user status omarkets` shows the service running; the log
    line `desk notes for NQ, ES, YM, GOLD, WTI, DXY on desk:v9` confirms it
    (with YM's, gold's, oil's and the dollar index's notes, above).
  - To keep the old behaviour, set both `OMARKETS_DESK_INSTRUMENTS=["NQ"]`
    and `OMARKETS_DESK_PROMPT_VERSION=desk:v8` in `.env`, then restart.
  - A `.env` or systemd drop-in that sets only `desk:v8` no longer starts
    (see below). Re-running `install-plugin` checks `.env` and warns; for a
    drop-in, `systemctl --user status omarkets` shows the refusal and which
    setting to change.
- Today and the bar popup name the instrument each note's tone is about
  ("Negative for ES"); notes about a calendar event stay "… for NQ".
- A fresh install now writes desk notes for NQ and ES (S&P 500 futures) with
  the `desk:v9` prompt, with no extra settings. ES gets move and ask notes;
  the daily pre-market stays NQ only. To go back to NQ only, set
  `OMARKETS_DESK_INSTRUMENTS=["NQ"]` (and `OMARKETS_DESK_PROMPT_VERSION=desk:v8`
  for the older prompt) in `.env`. YM joins the defaults too (above).
- If you set `OMARKETS_DESK_PROMPT_VERSION=desk:v8` on its own, also set
  `OMARKETS_DESK_INSTRUMENTS=["NQ"]`: desk:v8 has no ES prompt, so the
  service now refuses to start and says which setting to change.
- `install-plugin` prints the desk settings it starts with. It runs the
  service's startup check, so it warns when the service would refuse to
  start (for example ES or YM on a prompt with no text for them), and says
  when it couldn't check because `.venv` isn't there yet. Also,
  `omarchy/smoke-defaults [--live]` checks that a clean install runs on the
  defaults without a systemd drop-in.

## 0.1.1

- The installer now asks for your OpenRouter API key. `install-plugin` creates
  `.env` from `.env.example` if it's missing, asks you to paste the key
  (hidden input; press Enter to skip) and saves it to `.env` with owner-only
  permissions. It doesn't ask when a key is already set, when there's no
  terminal, or with `--no-prompt`. A key in the `OPENROUTER_API_KEY`
  environment variable is used instead of asking.
- New `omarchy/setup-env` script does the same on its own.

## 0.1.0

First public release. NQ (Nasdaq-100 futures) only.

- Today screen: delayed NQ quotes and related markets, with live, delayed and
  stale labels.
- Chart: NQ candles, key price levels and release markers.
- Economic calendar with forecasts and actuals from official sources.
- Macro page: inflation, jobs, growth and rates data.
- News from central banks, government sites and news feeds.
- Plain-English AI desk notes through OpenRouter, with spending caps.
- Learn library of the ideas the notes explain.
- Omarchy bar widget, popup, launcher and systemd user service.
- Renamed from MarketOS. Old `MARKETOS_*` settings and the `marketos`,
  `marketctl` and `marketos-mcp` commands still work. `install-plugin`
  migrates an existing MarketOS install.
