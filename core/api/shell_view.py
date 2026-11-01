"""Pure builders for the shell payloads.

These functions take already-computed deterministic state and map it to the
small, versioned shell contract. They perform no I/O and no calculation beyond
formatting; every number is copied from the source service.

The shell endpoints call these. Keeping the mapping pure means it is unit-tested
without a running app, and it guarantees the shell contract stays a *projection*
of the core rather than a second source of truth.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

from core.analysis.risk_tone import nq_effect, risk_tone
from core.domain.cross_market import Availability, CrossMarketState, Value
from core.domain.economics import EconomicEvent, Importance
from core.domain.instruments import PRIMARY_SYMBOL
from core.domain.models import DataStatus, FeedStatus, MarketState
from core.domain.sessions import SessionPhase, next_session_open, session_phase
from core.domain.shell import (
    ShellContext,
    ShellEvent,
    ShellHealth,
    ShellProviderHealth,
    ShellQuote,
    ShellRiskTone,
    ShellSession,
    ShellStatus,
)

NEW_YORK = ZoneInfo("America/New_York")

#: The glanceable bar strip with the default focus (NQ) and no saved watchlist.
STRIP_SYMBOLS: tuple[str, ...] = ("NQ", "US2Y", "VIX")
#: How many quotes the bar strip shows. Kept short on purpose.
STRIP_SIZE = len(STRIP_SYMBOLS)
#: What follows the focus on the bar until a watchlist is saved.
DEFAULT_COMPANIONS: tuple[str, ...] = STRIP_SYMBOLS[1:]
#: Popup cross-market strip, in display order.
CONTEXT_SYMBOLS: tuple[str, ...] = ("NQ", "ES", "US2Y", "US10Y", "DXY", "VIX", "VXN")
#: Short popup names (the instrument registry's names are long and formal).
SHORT_LABELS: dict[str, str] = {
    "NQ": "Nasdaq 100 futures",
    "ES": "S&P 500 futures",
    "YM": "Dow futures",
    "US2Y": "2-year Treasury yield",
    "US10Y": "10-year Treasury yield",
    "DXY": "US dollar index",
    "VIX": "S&P volatility index",
    "VXN": "Nasdaq volatility index",
}
#: Popup row groups, in display order.
GROUPS: dict[str, str] = {
    "NQ": "equities", "ES": "equities", "YM": "equities",
    "US2Y": "rates", "US10Y": "rates",
    "DXY": "dollar_volatility", "VIX": "dollar_volatility", "VXN": "dollar_volatility",
}
#: Event importances eligible for the bar/countdown.
BAR_IMPORTANCE: frozenset[str] = frozenset({Importance.HIGH.value, Importance.CRITICAL.value})


def focus_symbols(focus: str, symbols: Sequence[str], limit: int) -> list[str]:
    """``focus`` first, then ``symbols`` in order without repeating it, cut to ``limit``."""
    return [focus, *(s for s in symbols if s != focus)][:limit]


def bar_symbols(focus: str, watchlist: Sequence[str] | None = None) -> list[str]:
    """The bar strip: the focus, then the saved watchlist's first symbols.

    Without a saved watchlist the focus is followed by the 2-year yield and VIX,
    so the default (NQ) bar is the same ``NQ, US2Y, VIX`` it always was.
    """
    return focus_symbols(focus, DEFAULT_COMPANIONS if watchlist is None else watchlist,
                         STRIP_SIZE)


def _maybe(value: Value) -> float | None:
    """The scalar, or ``None`` when the core marked it unavailable."""
    return value.value if value.availability is Availability.AVAILABLE else None


def _presented(quote: ShellQuote, now: datetime | None) -> ShellQuote:
    """Attach the short label, group and (given ``now``) the effect on NQ.

    The effect is computed from the quote's own copied fields by the risk-tone
    rules, so the shell never judges a move itself.
    """
    update: dict[str, object] = {"short_label": SHORT_LABELS.get(quote.symbol),
                                 "group": GROUPS.get(quote.symbol)}
    if now is not None:
        update["nq_effect"] = nq_effect(quote.symbol, quote, now=now)
    return quote.model_copy(update=update)


def _quote_from_state(symbol: str, instrument) -> ShellQuote:
    return ShellQuote(
        symbol=symbol,
        label=instrument.display_name,
        value=instrument.value,
        change=instrument.change,
        change_pct=instrument.change_pct,
        feed_status=instrument.feed_status,
        status=instrument.status,
        market_closed=instrument.market_closed,
        as_of=instrument.timestamp,
        age_seconds=instrument.age_seconds,
    )


def build_strip(
    market: MarketState | None,
    symbols: Sequence[str],
    cross: CrossMarketState | None = None,
    *,
    now: datetime | None = None,
) -> list[ShellQuote]:
    """The glanceable bar strip from the market state, enriched with the
    deterministic basis-point move from the cross-market state for yields.

    The market observation carries no basis-point field (it is a price), so the
    bp is copied from the already-computed cross-market change, not recalculated.
    With ``now``, each quote also carries its effect on NQ.
    """
    if market is None:
        return []
    bp_by_symbol: dict[str, float] = {}
    if cross is not None:
        for symbol, asset in (
            ("US2Y", cross.us2y),
            ("US10Y", cross.us10y),
        ):
            bp_by_symbol[symbol] = _maybe(asset.change_bp)
    quotes: list[ShellQuote] = []
    for symbol in symbols:
        if symbol not in market.instruments:
            continue
        quote = _quote_from_state(symbol, market.instruments[symbol])
        bp = bp_by_symbol.get(symbol)
        if bp is not None:
            quote = quote.model_copy(update={"change_bp": bp})
        quotes.append(_presented(quote, now))
    return quotes


def build_cross_quotes(
    cross: CrossMarketState | None,
    symbols: Sequence[str],
    market: MarketState | None = None,
    *,
    now: datetime | None = None,
) -> list[ShellQuote]:
    """Cross-market quotes, using the pre-computed change basis points for yields.

    ``cross_market`` withholds relative-strength-derived NQ/ES changes when a leg
    is stale, but the market state still carries each instrument's own change. So
    the market state is preferred for value/change, and the cross-market state is
    used only to attach the deterministic basis-point move for yields.
    """
    if cross is None and market is None:
        return []
    if cross is None:
        return build_strip(market, symbols, now=now)
    change_pct_only = {
        "NQ": (cross.nq_change_pct, cross.nq_feed_status),
        "ES": (cross.es_change_pct, cross.es_feed_status),
    }
    assets = {
        "US2Y": cross.us2y,
        "US10Y": cross.us10y,
        "DXY": cross.dxy,
        "VIX": cross.vix,
        "VXN": cross.vxn,
    }
    quotes: list[ShellQuote] = []
    for symbol in symbols:
        # The market state is the authority for value/change when available.
        if market is not None and symbol in market.instruments:
            quote = _quote_from_state(symbol, market.instruments[symbol])
            if symbol in assets:
                quote = quote.model_copy(update={"change_bp": _maybe(assets[symbol].change_bp)})
            quotes.append(_presented(quote, now))
            continue
        if symbol in change_pct_only:
            change_pct, feed = change_pct_only[symbol]
            # Preserve the core's own classification: a delayed feed is a
            # freshness "stale" for display, but an unavailable feed must stay
            # unavailable rather than being relabelled as stale.
            if feed is FeedStatus.LIVE:
                status = DataStatus.LIVE
            elif feed is FeedStatus.UNAVAILABLE:
                status = DataStatus.UNAVAILABLE
            else:
                status = DataStatus.STALE
            quotes.append(_presented(
                ShellQuote(
                    symbol=symbol,
                    change_pct=_maybe(change_pct),
                    feed_status=feed,
                    status=status,
                ),
                now,
            ))
            continue
        asset = assets.get(symbol)
        if asset is None:
            continue
        quotes.append(_presented(
            ShellQuote(
                symbol=symbol,
                value=_maybe(asset.price),
                change=_maybe(asset.change),
                change_pct=_maybe(asset.change_pct),
                change_bp=_maybe(asset.change_bp),
                feed_status=asset.feed_status,
                status=asset.status,
                as_of=asset.timestamp,
            ),
            now,
        ))
    return quotes


def _fmt_et(moment: datetime | None, tz: ZoneInfo | None = None) -> str | None:
    if moment is None:
        return None
    zone = tz or NEW_YORK
    return moment.astimezone(zone).strftime("%H:%M %Z")


def zone_label(moment: datetime) -> str:
    """A short zone name ("EDT", "CET") or, when tzdata has none, "UTC+7"."""
    name = moment.strftime("%Z")
    if name.isalpha() and len(name) <= 5:
        return name
    offset = moment.utcoffset()
    if offset is None:
        return "UTC"
    minutes = int(offset.total_seconds()) // 60
    sign = "+" if minutes >= 0 else "-"
    hours, rest = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours}" + (f":{rest:02d}" if rest else "")


def fmt_local_short(moment: datetime, tz: ZoneInfo | None = None) -> str:
    """Format as "Mon 05:00 EDT": weekday, 24-hour time and a short zone, never a long name."""
    local = moment.astimezone(tz) if tz is not None else moment.astimezone()
    return f"{local.strftime('%a %H:%M')} {zone_label(local)}"


def build_event(event: EconomicEvent) -> ShellEvent:
    """Map one economic event, pre-rendering its ET and local display times.

    The core owns timezones so the shell never does tz math.
    """
    when = event.scheduled_at or event.released_at
    return ShellEvent(
        id=event.id,
        name=event.name,
        event_code=event.event_code,
        country=event.country,
        importance=event.importance.value,
        status=event.status.value,
        scheduled_at=when,
        time_et=_fmt_et(when),
        time_local=when.astimezone().strftime("%H:%M") if when is not None else None,
        actual=event.actual,
        consensus=event.consensus,
        previous=event.previous,
        unit=event.unit,
    )


def pick_next_event(events: Sequence[EconomicEvent], *, now: datetime) -> EconomicEvent | None:
    """The soonest scheduled HIGH/CRITICAL event still in the future.

    Scheduled time is authoritative; an event with no known time is skipped, so
    the countdown never counts to an invented time.
    """
    candidates = [
        event
        for event in events
        if event.scheduled_at is not None
        and event.scheduled_at >= now
        and event.importance.value in BAR_IMPORTANCE
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda event: event.scheduled_at)


def aggregate_health(
    *, market_healthy: bool, writer_ready: bool | None
) -> tuple[ShellHealth, ShellHealth]:
    """Derive the separate DATA and AI health badges.

    AI health never contributes to DATA health: a desk writer that cannot write
    must not make healthy market data read as down. ``writer_ready`` is ``None``
    when the desk is switched off.
    """
    data = ShellHealth.OK if market_healthy else ShellHealth.DOWN
    if writer_ready is None:
        return data, ShellHealth.DISABLED
    return data, ShellHealth.OK if writer_ready else ShellHealth.DEGRADED


def build_diagnostics(
    *,
    data_health: ShellHealth,
    ai_health: ShellHealth,
    market_detail: str | None,
    writer_detail: str | None,
    news_healthy: int,
    news_total: int,
    macro_ok: bool,
) -> list[ShellProviderHealth]:
    return [
        ShellProviderHealth(name="core", health=data_health, detail=market_detail),
        ShellProviderHealth(
            name="market",
            health=ShellHealth.OK if data_health is ShellHealth.OK else ShellHealth.DEGRADED,
            detail=market_detail,
        ),
        ShellProviderHealth(
            name="macro",
            health=ShellHealth.OK if macro_ok else ShellHealth.DEGRADED,
            detail=None,
        ),
        ShellProviderHealth(
            name="news",
            health=(
                ShellHealth.OK
                if news_total and news_healthy == news_total
                else ShellHealth.DEGRADED
                if news_total
                else ShellHealth.UNKNOWN
            ),
            detail=f"{news_healthy}/{news_total} sources",
        ),
        ShellProviderHealth(name="desk", health=ai_health, detail=writer_detail),
    ]


def build_session(now: datetime, tz: ZoneInfo | None = None) -> ShellSession:
    phase = session_phase(now)
    next_open = (
        next_session_open(now)
        if phase in (SessionPhase.CLOSED, SessionPhase.MAINTENANCE)
        else None
    )
    return ShellSession(
        phase=phase.value,
        maintenance_break=phase is SessionPhase.MAINTENANCE,
        next_open_at=next_open,
        next_open_local=fmt_local_short(next_open, tz) if next_open is not None else None,
    )


def build_status(
    *,
    now: datetime,
    market: MarketState | None,
    cross: CrossMarketState | None,
    events: Sequence[EconomicEvent],
    data_health: ShellHealth,
    ai_health: ShellHealth,
    providers: Sequence[ShellProviderHealth] = (),
    focus: str = PRIMARY_SYMBOL,
    watchlist: Sequence[str] | None = None,
) -> ShellStatus:
    """The bar payload. ``focus`` leads the strip and is the primary quote;
    ``watchlist`` is the saved watchlist (``None`` while the default applies)."""
    symbols = bar_symbols(focus, watchlist)
    strip = build_strip(market, symbols, cross, now=now)
    # Fall back to cross-market only when the market state itself is absent.
    if not strip and cross is not None:
        strip = build_cross_quotes(cross, symbols, now=now)
    # Only the focus's own quote is primary; never another instrument's.
    primary = next((quote for quote in strip if quote.symbol == focus), None)
    return ShellStatus(
        generated_at=now,
        focus=focus,
        primary=primary,
        strip=strip,
        session=build_session(now),
        next_event=build_event(event)
        if (event := pick_next_event(events, now=now)) is not None
        else None,
        risk_tone=build_risk_tone(market, now=now),
        data_health=data_health,
        ai_health=ai_health,
        providers=list(providers),
    )


def build_risk_tone(market: MarketState | None, *, now: datetime) -> ShellRiskTone | None:
    """The risk tone with its strongest reason, copied from the core's rules."""
    if market is None:
        return None
    tone = risk_tone(market, now=now)
    reason = tone.reasons[0] if tone.reasons and tone.tone != "unclear" else (
        "not enough fresh quotes to judge.")
    return ShellRiskTone(tone=tone.tone, label=tone.label,
                         line=f"Risk tone: {tone.label} · {reason}")


def build_context(
    *,
    now: datetime,
    cross: CrossMarketState | None,
    events: Sequence[EconomicEvent],
    diagnostics: Sequence[ShellProviderHealth],
    market: MarketState | None = None,
    focus: str = PRIMARY_SYMBOL,
) -> ShellContext:
    # Only genuinely future events, so the popup never lists a stale stored
    # release as "upcoming".
    upcoming = sorted(
        (event for event in events if event.scheduled_at is not None and event.scheduled_at >= now),
        key=lambda event: event.scheduled_at,
    )[:5]
    return ShellContext(
        generated_at=now,
        cross_market=build_cross_quotes(
            cross, focus_symbols(focus, CONTEXT_SYMBOLS, len(CONTEXT_SYMBOLS) + 1), market,
            now=now),
        upcoming_events=[build_event(event) for event in upcoming],
        diagnostics=list(diagnostics),
    )


__all__ = [
    "BAR_IMPORTANCE",
    "CONTEXT_SYMBOLS",
    "DEFAULT_COMPANIONS",
    "GROUPS",
    "SHORT_LABELS",
    "STRIP_SIZE",
    "STRIP_SYMBOLS",
    "aggregate_health",
    "bar_symbols",
    "build_context",
    "build_cross_quotes",
    "build_diagnostics",
    "build_event",
    "build_risk_tone",
    "build_session",
    "build_status",
    "build_strip",
    "focus_symbols",
    "pick_next_event",
]
