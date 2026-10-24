# Omarkets

**A plain-English market desk for Nasdaq-100 futures (NQ), built into the Omarchy desktop.**

Omarkets shows delayed NQ quotes, a chart, an economic calendar, macro data
and the news. It writes short desk notes in plain English with AI (through
OpenRouter): before the open, after each data release, after Fed events, after
big moves and at the end of the week. It is meant for people learning how
macro news moves markets.

Numbers come from ordinary, tested code. The AI only explains them.

> **Not financial advice.** Omarkets never places orders and never tells you
> what to trade. See the [disclaimer](#disclaimer).

Version 0.1.0 covers NQ only.

![Omarkets demo: Today, chart, calendar, macro, news, desk notes and the bar popup](docs/assets/omarkets-demo.gif)

## What you get

- **Today**: NQ and related markets (S&P 500 futures, yields, dollar, VIX,
  oil, gold) and the latest desk notes.
- **Chart**: NQ candles with key price levels and economic releases marked.
- **Calendar**: this week's and next week's US releases, with forecasts and
  actuals.
- **Macro**: inflation, jobs, growth and rates data from official sources.
- **News**: headlines from central banks, government sites and news feeds.
- **Learn**: a library of the ideas the notes explain.
- **Bar widget**: an icon in the Omarchy bar with a small market popup.
- **Notifications**: a desktop alert when a new note is ready.

Every quote is labelled **live**, **delayed** or **stale**.

## Requirements

- [Omarchy](https://omarchy.org) (Arch Linux + Hyprland)
- Python 3.12 or newer
- Node.js 22.13 or newer (to build the web app with Vite 8)
- An [OpenRouter](https://openrouter.ai) API key for the desk notes

Optional free API keys: [FRED](https://fred.stlouisfed.org/docs/api/api_key.html),
[BLS](https://data.bls.gov/registrationEngine/) and
[Census](https://api.census.gov/data/key_signup.html). Without them, the macro
data from those sources is simply not shown.

## Install

```bash
git clone https://github.com/neilmc81/omarkets.git
cd omarkets

# Python core
python3 -m venv .venv
.venv/bin/pip install -e .

# Web app
(cd web && npm ci && npm run build)

# Settings: add your OpenRouter key
cp .env.example .env
$EDITOR .env

# Bar widget, launcher and the systemd user service
./omarchy/install-plugin
```

`install-plugin` installs `omarkets.service` for your checkout and starts it.
If you used `--no-service`, enable it yourself:

```bash
systemctl --user enable --now omarkets.service
```

Then open <http://127.0.0.1:8765> or click the bar icon. Add `--bindings` to
`install-plugin` for hotkeys (`SUPER+M` opens Omarkets). Run
`./omarchy/install-plugin --dry-run` to see what it would change first.

To remove it: `./omarchy/uninstall-plugin`. Your data in `data/` is kept.

## Configuration

Settings live in `.env` (see [`.env.example`](.env.example)). The main ones:

| Variable | What it does |
| --- | --- |
| `OPENROUTER_API_KEY` | Required for desk notes. |
| `OMARKETS_FRED_API_KEY`, `OMARKETS_BLS_API_KEY`, `OMARKETS_CENSUS_API_KEY` | Optional data keys. |
| `OMARKETS_AI_WEEKLY_BUDGET_USD` | Weekly AI spending cap (0 = off). |
| `OMARKETS_AI_MONTHLY_BUDGET_USD`, `OMARKETS_AI_DAILY_BUDGET_USD` | Monthly and daily caps (defaults $5 and $0.20). |
| `OMARKETS_AI_BUDGET_TIMEZONE` | Where budget days and weeks start. Auto-detected from the system; set to override. |
| `OMARKETS_DESK_LOCAL_TIMEZONE` | Your time zone, for the daily plan. Auto-detected from the system (`TZ`, then `/etc/localtime`, else UTC); set to override. Market hours stay in New York time. |
| `OMARKETS_DESK_MODELS` | Pick the OpenRouter model per note type. |
| `OMARKETS_DESK_NOTIFY` | Desktop notifications on or off. |

Old `MARKETOS_*` names still work. If both are set, `OMARKETS_*` wins.

## Costs

The software is free. AI notes cost about **$1.60 a month** in OpenRouter
fees with the default models (a strong model for the pre-market note and the
weekly review, a cheap one for the rest). This is a rough estimate; it depends
on how much news there is. Spending caps stop writing before a cap is passed.

## Data sources

Omarkets fetches data from public sources on your computer, at runtime.
Nothing is bundled or redistributed. Each source's own terms apply.

- **Quotes**: CNBC's public quote service (futures about 10 minutes delayed).
- **Economic data**: FRED (St. Louis Fed), Bureau of Labor Statistics, Census
  Bureau, Bureau of Economic Analysis, Department of Labor, Federal Reserve
  Board, New York Fed, US Treasury (auctions), EIA, ADP, ISM (via PR
  Newswire), National Association of Realtors.
- **Calendar forecasts**: the Forex Factory weekly calendar feed.
- **Earnings dates**: Nasdaq.
- **News**: RSS feeds from central banks (Fed, ECB, Bank of England, Bank of
  Japan), US government sites, the European Commission, CNBC, BBC, NPR and
  The Guardian.

Quotes and data can be late, missing or wrong. Always check a primary source.

This product uses the FRED® API but is not endorsed or certified by the
Federal Reserve Bank of St. Louis.

This product uses the Census Bureau Data API but is not endorsed or certified
by the Census Bureau.

BLS.gov cannot vouch for the data or analyses derived from these data after
the data have been retrieved from BLS.gov.

## Roadmap

1. ES and YM (S&P 500 and Dow futures)
2. European indices: Euro Stoxx 50, DAX 40, FTSE 100, CAC 40, SMI, IBEX 35,
   FTSE MIB, AEX, Stoxx 600
3. Forex
4. Crypto
5. US stocks

## Disclaimer

Omarkets is for education only. It is not financial advice. It never places
orders. Quotes are delayed and come from free sources; they may be wrong or
late. AI notes can be mistaken. You are responsible for your own decisions.

## Credits and licences

Omarkets is released under the [MIT licence](LICENSE).

- Charts by [TradingView](https://www.tradingview.com/) Lightweight Charts
  (Apache-2.0).
- The bar icon is `nf-md-finance` from Material Design Icons, drawn by the
  Nerd Font already installed on Omarchy. No font is bundled.

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for details.

## More docs

- [Desk design](docs/desk.md): what is written when, and why
- [Architecture](docs/architecture.md)
- [Reference](docs/reference.md): settings, API and scripts
