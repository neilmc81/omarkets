# Third-party notices

Omarkets is MIT-licensed (see [LICENSE](LICENSE)). It uses the software and
data services below. Their own licences and terms apply.

## TradingView Lightweight Charts

- Package: `lightweight-charts` (installed by npm, bundled into `web/dist`)
- Licence: Apache License 2.0
- Copyright: TradingView, Inc.
- Website: <https://www.tradingview.com/>

The Lightweight Charts NOTICE asks for attribution with a link to
tradingview.com. Omarkets turns the chart's own logo off
(`attributionLogo: false` in `web/src/chart/SessionChart.tsx`) and instead
shows a visible **"Charts by TradingView"** link under every chart, linking to
<https://www.tradingview.com/>. The System status panel's About section
repeats it.

## Bar icon

The Omarchy bar icon is `nf-md-finance` (U+F081F) from Material Design Icons
(Pictogrammers, Apache License 2.0). It is drawn by the Nerd Font installed on
the user's system. Omarkets does not bundle any font or icon file.

## Data services

Omarkets fetches data from public sources at runtime on the user's computer.
No data is bundled or redistributed. Each source's terms of use apply.

**FRED.** This product uses the FRED® API but is not endorsed or certified by
the Federal Reserve Bank of St. Louis.

**U.S. Census Bureau.** This product uses the Census Bureau Data API but is not
endorsed or certified by the Census Bureau.

**U.S. Bureau of Labor Statistics.** BLS.gov cannot vouch for the data or
analyses derived from these data after the data have been retrieved from
BLS.gov.

Other sources (quotes, calendar forecasts, earnings dates, news feeds and
issuer releases) are listed in the README under "Data sources".

## Other open-source dependencies

The Python core uses FastAPI, Uvicorn, Pydantic, pydantic-settings, DuckDB,
pytz, HTTPX, icalendar and defusedxml. The web app uses React, React DOM and
Zod. All are under permissive licences (MIT, BSD or Apache-2.0); see each
package for its licence text.
