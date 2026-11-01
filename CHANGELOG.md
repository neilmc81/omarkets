# Changelog

## Unreleased

- **The US Dollar Index (DXY) now gets desk notes by default.** The default
  `desk_instruments` is `["NQ", "ES", "YM", "GOLD", "WTI", "DXY"]` on
  `desk:v9`. DXY works like gold: its own move notes (with its own 3-hour
  limit and move trigger) and "Explain the market now" notes when DXY is the
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
  NQ, ES, YM, gold and oil notes are unchanged. That means a few more notes
  and a little more AI spend each week, though a one-hour move big enough to
  trigger a dollar note is rare. To leave DXY out, set
  `OMARKETS_DESK_INSTRUMENTS=["NQ", "ES", "YM", "GOLD", "WTI"]` in `.env` and
  restart; listing DXY with `desk:v8` refuses to start. After updating, the
  log line reads `desk notes for NQ, ES, YM, GOLD, WTI, DXY on desk:v9`.
- **Crude oil (WTI) now gets desk notes by default.** The default
  `desk_instruments` is `["NQ", "ES", "YM", "GOLD", "WTI"]` on `desk:v9`. Oil
  works like gold: its own move notes (with its own 3-hour limit and move
  trigger) and "Explain the market now" notes when WTI is the focus. There is
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
  notes (with its own 3-hour limit and move trigger) and "Explain the market
  now" notes when GOLD is the focus. There is no gold pre-market; only NQ gets
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
  "Explain the market now" notes when YM is the focus. There is no YM
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
  prices"; delayed quotes are labelled (once in the footer when all are
  delayed, per row when the feeds are mixed). After updating, run
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
