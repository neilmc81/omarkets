# Changelog

## Unreleased

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
    line `desk notes for NQ, ES on desk:v9` confirms it.
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
  for the older prompt) in `.env`. YM has no desk notes yet.
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
