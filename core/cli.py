"""``omarketsctl`` — a minimal operator CLI for Omarkets.

Phase 1 scope: ``status`` and ``state`` against a running local API. This is a
convenience wrapper; it contains no market logic and reads only the public API.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

from core.config.settings import Settings


def _get(settings: Settings, path: str) -> dict:
    url = f"http://{settings.api_host}:{settings.api_port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        print(f"omarketsctl: cannot reach Omarkets API at {url}: {exc.reason}", file=sys.stderr)
        raise SystemExit(2) from exc


def _cmd_status(settings: Settings) -> int:
    health = _get(settings, "/health")
    print(f"status:   {health['status']}")
    print(f"version:  {health['app']} {health['version']}")
    print(f"time:     {health['time']}")
    print(f"database: {'ok' if health['database_ok'] else 'FAIL'}")
    print(f"provider: {health['provider']} ({health['provider_status']})")
    print(f"stream:   {'running' if health['stream_running'] else 'stopped'}")
    print(f"updates:  {health['updates_published']}")
    print(f"clients:  {health['subscriber_count']}")
    return 0 if health["status"] == "ok" else 1


def _cmd_state(settings: Settings, symbol: str | None) -> int:
    if symbol:
        payload = _get(settings, f"/api/v1/instruments/{symbol}")
        state = payload["state"]
        print(
            f"{state['symbol']:<6} {_value(state):>12} "
            f"{_pct(state):>8} {state['status']:<11} "
            f"{state['timestamp'] or '-'}"
        )
        return 0

    payload = _get(settings, "/api/v1/state")
    print(
        f"generated_at: {payload['generated_at']}  provider: {payload['provider']} "
        f"({payload['provider_status']})"
    )
    print(
        f"live={payload['live_count']} stale={payload['stale_count']} "
        f"unavailable={payload['unavailable_count']}"
    )
    for state in payload["instruments"].values():
        print(
            f"{state['symbol']:<6} {_value(state):>12} "
            f"{_pct(state):>8} {state['status']:<11} "
            f"{state['timestamp'] or '-'}"
        )
    return 0


def _value(state: dict) -> str:
    return "-" if state["value"] is None else f"{state['value']:g}"


def _pct(state: dict) -> str:
    return "-" if state["change_pct"] is None else f"{state['change_pct']:+.2f}%"


def _cmd_macro(settings: Settings, series_id: str | None) -> int:
    if series_id:
        payload = _get(settings, f"/api/v1/macro/series/{series_id}")
        definition = payload["definition"]
        print(f"{definition['id']:<24} {definition['name']} ({definition['unit']})")
        for item in payload["observations"]:
            print(f"  {item['period']:<10} {_num(item['value']):>12}  r{item['revision']}")
        return 0
    payload = _get(settings, "/api/v1/macro/state")
    print(f"generated_at: {payload['generated_at']}  mode: {payload['mode']}")
    for item in payload["series"]:
        latest = item["latest"]
        value = "-" if latest is None or latest["value"] is None else f"{latest['value']:g}"
        print(
            f"{item['definition']['id']:<24} {value:>12} {item['status']:<12} "
            f"{(latest['period'] if latest else '-'):<10} "
            f"{item['last_checked_at'] or '-'}"
        )
    return 0


def _num(value: float | None) -> str:
    return "-" if value is None else f"{value:g}"


def _cmd_calendar(settings: Settings, scope: str) -> int:
    payload = _get(settings, f"/api/v1/calendar/{scope}")
    print(f"generated_at: {payload['generated_at']}  mode: {payload['mode']}")
    print(f"coverage: {'; '.join(payload['coverage'])}")
    for item in payload["events"]:
        when = item["scheduled_at"] or "-"
        print(
            f"{when:<25} {item['importance']:<8} {item['category']:<12} "
            f"{item['status']:<10} {item['name']}"
        )
    for item in payload["providers"]:
        print(f"provider: {item['name']} ({item['status']})")
    return 0


def _cmd_next_event(settings: Settings) -> int:
    payload = _get(settings, "/api/v1/calendar/upcoming?status=scheduled&limit=1")
    events = payload["events"]
    if not events:
        print("no upcoming events with known scheduled times")
        return 0
    item = events[0]
    print(f"{item['scheduled_at']}  {item['importance']:<8} {item['name']}")
    return 0


def _cmd_providers(settings: Settings) -> int:
    payload = _get(settings, "/api/v1/providers")
    print(f"generated_at: {payload['generated_at']}  mode: {payload['mode']}")
    for item in payload["providers"]:
        detail = f" — {item['detail']}" if item.get("detail") else ""
        print(f"{item['name']:<16} {item['status']}{detail}")
    return 0


def _cmd_markets(settings: Settings) -> int:
    payload = _get(settings, "/api/v1/markets/status")
    print(f"generated_at: {payload['generated_at']}")
    for row in payload["markets"]:
        value = "-" if row["value"] is None else f"{row['value']:g}"
        change = "-" if row["change"] is None else f"{row['change']:+g}"
        age = "-" if row["age_seconds"] is None else f"{row['age_seconds']:.0f}s"
        feed = row["feed_status"].upper()
        print(
            f"{row['symbol']:<6} {value:>12} {change:>10} {feed:<11} "
            f"{row['source'] or '-':<8} {age:>6} {row['provider_symbol'] or ''}"
        )
    return 0


def _cmd_quote(settings: Settings, symbol: str) -> int:
    payload = _get(settings, f"/api/v1/instruments/{symbol}")
    state = payload["state"]
    print(f"{state['symbol']}  {payload['display_name']}")
    print(f"value:    {_value(state)}")
    print(f"change:   {_pct(state)}")
    print(f"status:   {state['status']}  feed: {state.get('feed_status', 'unavailable')}")
    print(f"source:   {state['source'] or '-'}")
    if state.get("provider_symbol"):
        print(f"provider: {state['provider_symbol']}")
    if state.get("contract_code"):
        print(f"contract: {state['contract_code']} (expires {state.get('contract_expiry')})")
    print(f"exchange: {state['timestamp'] or '-'}")
    print(f"received: {state['received_at'] or '-'}")
    age = state.get("age_seconds")
    print(f"age:      {'-' if age is None else f'{age:.1f}s'}")
    return 0


def _cmd_contracts(settings: Settings) -> int:
    payload = _get(settings, "/api/v1/contracts")
    print(f"generated_at: {payload['generated_at']}")
    for item in payload["contracts"]:
        print(
            f"{item['symbol']:<5} {item['contract_code']:<8} expiry {item['expiry']} "
            f"provider {item['provider_symbol']}"
        )
    return 0


def _cmd_market_providers(settings: Settings) -> int:
    payload = _get(settings, "/api/v1/market/providers")
    print(
        f"generated_at: {payload['generated_at']}  mode: {payload['provider_mode']} "
        f"({payload['provider_status']})"
    )
    for route in payload["routes"]:
        print(
            f"{route['symbol']:<6} {route['provider']:<10} "
            f"{route['feed_status']:<11} {route['source'] or '-'}"
        )
    return 0


def _value_str(value: float | None) -> str:
    return "-" if value is None else f"{value:g}"


def _available(container: dict) -> tuple[bool, float | None]:
    return (
        container.get("availability") == "available" and container.get("value") is not None,
        container.get("value"),
    )


def _cmd_cross_market(settings: Settings) -> int:
    payload = _get(settings, "/api/v1/cross-market")

    def line(label: str, change: dict, suffix: str = "%") -> str:
        ok, value = _available(change)
        return f"{label:<10} {_value_str(value) + suffix if ok else 'unavailable'}"

    ok, nq = _available(payload["nq_change_pct"])
    ok2, es = _available(payload["es_change_pct"])
    print(f"generated:  {payload['generated_at']}")
    print(f"NQ:         {_value_str(nq) + '%' if ok else 'unavailable'}")
    print(f"ES:         {_value_str(es) + '%' if ok2 else 'unavailable'}")
    ok, value = _available(payload["nq_vs_es"])
    print(
        f"NQ vs ES:   {_value_str(value) + 'pp' if ok else 'unavailable'}  "
        f"({payload['nq_vs_es_label']})"
    )
    print()
    print(line("US2Y bp", payload["us2y"]["change_bp"], "bp"))
    print(line("US10Y bp", payload["us10y"]["change_bp"], "bp"))
    print(line("DXY", payload["dxy"]["change_pct"]))
    print(line("VIX", payload["vix"]["change_pct"]))
    print(line("VXN", payload["vxn"]["change_pct"]))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="omarketsctl", description="Omarkets operator CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show application and provider health")
    state = sub.add_parser("state", help="show current market state")
    state.add_argument("symbol", nargs="?", help="limit output to one symbol")
    macro = sub.add_parser("macro", help="show macro series state or one series' history")
    macro.add_argument("series_id", nargs="?", help="limit output to one series")
    calendar = sub.add_parser("calendar", help="show the economic calendar")
    calendar.add_argument(
        "scope", nargs="?", choices=["today", "week", "upcoming"], default="today"
    )
    sub.add_parser("next-event", help="show the next upcoming economic event")
    sub.add_parser("providers", help="show economic provider health")
    sub.add_parser("markets", help="show every market instrument's value and feed quality")
    quote = sub.add_parser("quote", help="show one market instrument in detail")
    quote.add_argument("symbol")
    sub.add_parser("contracts", help="show active futures contracts")
    sub.add_parser("market-providers", help="show market-data provider routing")
    sub.add_parser("cross-market", help="show NQ cross-market context")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings()
    commands = {
        "status": lambda: _cmd_status(settings),
        "state": lambda: _cmd_state(settings, args.symbol),
        "macro": lambda: _cmd_macro(settings, args.series_id),
        "calendar": lambda: _cmd_calendar(settings, args.scope),
        "next-event": lambda: _cmd_next_event(settings),
        "providers": lambda: _cmd_providers(settings),
        "markets": lambda: _cmd_markets(settings),
        "quote": lambda: _cmd_quote(settings, args.symbol),
        "contracts": lambda: _cmd_contracts(settings),
        "market-providers": lambda: _cmd_market_providers(settings),
        "cross-market": lambda: _cmd_cross_market(settings),
    }
    return commands[args.command]()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
