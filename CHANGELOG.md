# Changelog

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
