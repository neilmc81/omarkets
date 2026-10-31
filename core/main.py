"""Omarkets application entrypoint.

Wires configuration, storage, provider, state service, and the FastAPI app
together, then runs Uvicorn bound to localhost. All construction happens here
so there is no import-time side effect and tests can build their own graph.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

import uvicorn

from core.api.app import build_service, create_app
from core.config.logging import configure_logging
from core.config.settings import Settings
from core.domain.economics import EconomicsMode, MacroRegistry
from core.domain.instruments import PRIMARY_SYMBOL, all_symbols
from core.domain.news import SourceMetadata, SourceType
from core.providers.base import MarketDataProvider, ProviderError
from core.providers.binance import BinanceCryptoProvider
from core.providers.cnbc import CNBCMarketDataProvider
from core.providers.composite import CompositeMarketDataProvider
from core.providers.fake import FakeMarketDataProvider
from core.providers.news.economic_calendar import EconomicCalendarSource
from core.providers.registry import DEFAULT_ROUTES, ProviderRegistry
from core.services.economics import EconomicPolling, EconomicService
from core.services.news import NewsPolling, NewsService, build_sources
from core.storage.base import ObservationRepository
from core.storage.duckdb_repo import DuckDBObservationRepository
from core.storage.economics import SQLiteEconomicRepository
from core.storage.events import SQLiteEventRepository
from core.storage.workspace import WorkspaceStore
from core.theme.omarchy import OmarchyTheme

logger = logging.getLogger(__name__)


def _secret(value) -> str | None:
    return value.get_secret_value() if value is not None else None


def build_provider(settings: Settings) -> MarketDataProvider:
    """Construct the configured provider adapter.

    ``market_data_mode=demo`` always uses the deterministic synthetic provider.
    ``live`` composes the real adapters. A failed live feed is never silently
    replaced by demo data.
    """
    requested = (settings.data_provider or "").strip().lower()
    mode = settings.market_data_mode
    if requested:
        if requested == "fake":
            return FakeMarketDataProvider(
                seed=settings.provider_seed,
                update_interval=settings.provider_update_interval,
            )
        if requested == "cnbc":
            return CNBCMarketDataProvider(
                update_interval=settings.market_poll_seconds,
                futures_roll_days=settings.futures_roll_days,
                live_lag_seconds=settings.market_live_lag_seconds,
            )
        raise ProviderError(requested, "unknown market data provider; use 'cnbc' or 'fake'")
    if mode == "demo":
        return FakeMarketDataProvider(
            seed=settings.provider_seed,
            update_interval=settings.provider_update_interval,
        )
    registry = build_registry()
    providers: list[MarketDataProvider] = []
    if registry.symbols_for("cnbc"):
        providers.append(
            CNBCMarketDataProvider(
                symbols=tuple(registry.symbols_for("cnbc")),
                update_interval=settings.market_poll_seconds,
                futures_roll_days=settings.futures_roll_days,
                live_lag_seconds=settings.market_live_lag_seconds,
            )
        )
    if registry.symbols_for("binance"):
        providers.append(BinanceCryptoProvider(update_interval=settings.market_poll_seconds * 2))
    if not providers:
        raise ProviderError("live", "no market data providers are configured for live mode")
    return CompositeMarketDataProvider(providers) if len(providers) > 1 else providers[0]


def build_registry(overrides: dict[str, str] | None = None) -> ProviderRegistry:
    routes = dict(DEFAULT_ROUTES)
    if overrides:
        routes.update(overrides)
    return ProviderRegistry(routes)


def build_repository(settings: Settings) -> ObservationRepository:
    """Construct the configured storage backend.

    Live mode performs a one-time, non-destructive migration of real
    observations from the pre-namespacing shared database, so switching to the
    per-mode file does not lose genuine history. Synthetic rows are excluded.
    """
    target = settings.resolved_duckdb_path
    default_live_path = settings.default_duckdb_path
    if (
        settings.market_data_mode == "live"
        and target == default_live_path
        and not target.exists()
        and settings.legacy_duckdb_path.exists()
    ):
        try:
            migrated = DuckDBObservationRepository.migrate_observations(
                source_path=settings.legacy_duckdb_path,
                target_path=target,
            )
            if migrated:
                logger.info(
                    "migrated %d real observation(s) from %s into %s",
                    migrated,
                    settings.legacy_duckdb_path,
                    target,
                )
        except Exception:  # pragma: no cover - migration must not block startup
            logger.exception("observation migration failed; continuing with a fresh live database")
    return DuckDBObservationRepository(target)


def build_economic_service(settings: Settings) -> EconomicService:
    mode = settings.economic_mode
    if mode == EconomicsMode.DISABLED:
        return EconomicService(mode=mode)
    repository = SQLiteEconomicRepository(settings.resolved_economic_db_path, mode)
    from core.domain.macro_registry import MACRO_DEFINITIONS
    from core.providers.economic_factory import build_economic_providers

    definitions = MACRO_DEFINITIONS
    if mode == EconomicsMode.DEMO:
        definitions = tuple(
            item.model_copy(update={"provider": "demo", "source_series_id": "demo:CPI_MOM"})
            for item in definitions
            if item.id == "CPI_MOM"
        )
    return EconomicService(
        repository=repository,
        providers=build_economic_providers(
            mode=mode,
            fred_api_key=_secret(settings.fred_api_key),
            bls_api_key=_secret(settings.bls_api_key),
            census_api_key=_secret(settings.census_api_key),
            bea_api_key=_secret(settings.bea_api_key),
        ),
        registry=MacroRegistry(definitions=definitions),
        mode=mode,
        polling=EconomicPolling(
            default_seconds=settings.economic_poll_seconds,
            overrides=settings.economic_poll_overrides,
            transition_seconds=settings.economic_transition_seconds,
            proximity_seconds=settings.economic_proximity_seconds,
            proximity_before_seconds=settings.economic_proximity_before_seconds,
            proximity_after_seconds=settings.economic_proximity_after_seconds,
        ),
    )


def build_news_service(
    settings: Settings,
    *,
    economic_service: EconomicService | None = None,
) -> NewsService:
    """Construct the news/geopolitical ingestion service.

    The economic-calendar source is an adapter over the Phase 2 economics
    service when one is available; it never recomputes macro values. When no
    economics service is configured, the calendar bridge is omitted.
    """
    from core.providers.news.base import utc_now

    if not settings.news_enabled:
        return NewsService(enabled=False)
    sources = build_sources(
        enabled=settings.news_enabled_sources or None,
        disabled=settings.news_disabled_sources,
        clock=utc_now,
    )
    if economic_service is not None and economic_service.mode != EconomicsMode.DISABLED:
        sources.append(
            EconomicCalendarSource(
                events=lambda: (
                    *economic_service.calendar_events(
                        start=utc_now() - timedelta(days=1),
                        end=utc_now() + timedelta(seconds=1),
                    ),
                ),
                metadata=SourceMetadata(
                    name="economic_calendar",
                    display_name="Omarkets · Economic Calendar",
                    source_type=SourceType.ECONOMIC_CALENDAR,
                    official=False,
                    reliable=True,
                    language="en",
                    attribution="Phase 2 macro calendar and observed releases",
                    notes=[
                        "Adapts Phase 2 calendar/observed releases into the event stream; "
                        "no macro value is recomputed."
                    ],
                ),
                clock=utc_now,
            )
        )
    return NewsService(
        repository=SQLiteEventRepository(settings.resolved_news_db_path, "live"),
        sources=sources,
        polling=NewsPolling.from_catalog(
            settings.news_poll_seconds, settings.news_poll_overrides
        ),
        clock=utc_now,
        similarity_threshold=settings.news_dedupe_threshold,
        dedupe_window=timedelta(hours=settings.news_dedupe_window_hours),
    )


class DeskConfigError(RuntimeError):
    """The desk settings cannot run together; the service refuses to start."""


def check_desk_instruments(settings: Settings) -> tuple[str, ...]:
    """The instruments the desk writes for, refused when one has no prompt
    profile at ``desk_prompt_version`` (desk:v8 is NQ's text only, so ES needs
    desk:v9)."""
    from core.desk.prompts import VERSIONS, has_profile

    version = settings.desk_prompt_version
    instruments = tuple(settings.desk_instruments)
    missing = [s for s in instruments if not has_profile(s, version=version)]
    if not missing:
        return instruments
    names = ", ".join(missing)
    fits = [v for v in VERSIONS if all(has_profile(s, version=v) for s in instruments)]
    fix = (f"set OMARKETS_DESK_PROMPT_VERSION={fits[-1]} or remove {names} from "
           "OMARKETS_DESK_INSTRUMENTS" if fits
           else f"remove {names} from OMARKETS_DESK_INSTRUMENTS")
    raise DeskConfigError(
        f"desk_instruments includes {names}, but {version} has no prompt for "
        f"{'it' if len(missing) == 1 else 'them'}. To fix: {fix}. Run "
        "`omarketsctl config` to see where each setting comes from.")


def build_desk_service(settings: Settings, *, market, economics, news_service, cost_guard):
    """Plain-English desk notes: the Today screen and its notifications."""
    from core.desk.models import NoteKind
    from core.desk.service import DeskService, DeskSources
    from core.desk.store import DeskStore
    from core.desk.writer import DeskWriter
    from core.notifications.notifier import NotifySendNotifier
    from core.providers.earnings_nasdaq import NasdaqEarningsCalendar
    from core.providers.treasury_auctions import TreasuryAuctions

    def calendar_events(start, end):
        if economics is None or economics.mode.value == "disabled":
            return []
        return economics.calendar_events(start=start, end=end, limit=2000)

    def news_events():
        return [] if news_service is None else news_service.latest_events(limit=500)

    def macro_series():
        if economics is None or economics.mode.value == "disabled":
            return []
        return economics.current_state().series

    instruments = check_desk_instruments(settings)
    notifier = NotifySendNotifier()

    async def notify(summary: str, body: str) -> None:
        await notifier.deliver_text(summary, body)

    writer = DeskWriter(
        api_key=_secret(settings.openrouter_api_key),
        models={kind: settings.desk_model_for(kind) for kind in NoteKind},
        fallback_model=settings.desk_fallback_model,
        model_params=settings.resolved_desk_model_params,
        model_rates=settings.resolved_desk_model_rates,
        input_rate=settings.desk_input_rate,
        output_rate=settings.desk_output_rate,
        cost_guard=cost_guard,
        prompt_version=settings.desk_prompt_version,
    )
    return DeskService(
        sources=DeskSources(
            market_state=market.current_state,
            history=market.history,
            calendar_events=calendar_events,
            news_events=news_events,
            ohlc=market.ohlc,
            macro_series=macro_series,
        ),
        store=DeskStore(settings.resolved_desk_db_path),
        writer=writer,
        earnings=NasdaqEarningsCalendar(),
        auctions=TreasuryAuctions(),
        notify=notify if settings.desk_notify else None,
        move_threshold_pct=settings.desk_move_threshold_pct,
        enabled=settings.desk_enabled,
        local_tz=ZoneInfo(settings.desk_local_timezone),
        daily_plan=settings.desk_daily_plan,
        instruments=instruments,
    )


def build_app(settings: Settings):
    """Build the full application graph."""
    from core.notifications.notifier import NotifySendNotifier
    from core.services.ai_budget import AICostGuard

    if settings.desk_enabled:
        check_desk_instruments(settings)  # before any store or provider is opened
    cost_guard = AICostGuard(
        settings.resolved_ai_budget_db_path,
        monthly_limit_usd=settings.ai_monthly_budget_usd,
        daily_limit_usd=settings.ai_daily_budget_usd,
        weekly_limit_usd=settings.ai_weekly_budget_usd,
        timezone=settings.ai_budget_timezone,
        openrouter_daily_request_limit=settings.ai_openrouter_daily_request_limit,
        openrouter_background_request_limit=settings.ai_openrouter_background_request_limit,
        notifier=NotifySendNotifier(),
    )
    provider = build_provider(settings)
    repository = build_repository(settings)
    if not provider.instruments:  # pragma: no cover - defensive
        raise ProviderError(provider.name, "provider exposes no instruments")
    service = build_service(
        settings=settings,
        provider=provider,
        repository=repository,
    )
    economic_service = build_economic_service(settings)
    news_service = build_news_service(settings, economic_service=economic_service)
    desk_service = (
        build_desk_service(
            settings,
            market=service,
            economics=economic_service,
            news_service=news_service,
            cost_guard=cost_guard,
        )
        if settings.desk_enabled
        else None
    )
    return create_app(
        service=service,
        economic_service=economic_service,
        desk_service=desk_service,
        theme_source=OmarchyTheme(settings.omarchy_theme_dir),
        news_service=news_service,
        ai_budget=cost_guard,
        workspace_store=WorkspaceStore(settings.resolved_workspace_db_path),
        frontend_dist=(settings.resolved_frontend_dist if settings.serve_frontend else None),
    )


def main() -> None:
    """Run the Omarkets API server."""
    settings = Settings()
    configure_logging(settings.log_level)

    logger.info(
        "Omarkets starting (provider=%s, db=%s, %s:%d)",
        settings.data_provider,
        settings.resolved_duckdb_path,
        settings.api_host,
        settings.api_port,
    )
    logger.info(
        "instrument universe: %d symbols, primary=%s",
        len(all_symbols()),
        PRIMARY_SYMBOL,
    )

    try:
        app = build_app(settings)
    except DeskConfigError as exc:
        logger.error("refusing to start: %s", exc)
        raise SystemExit(f"omarkets: {exc}") from exc
    config = uvicorn.Config(
        app,
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
        workers=1,
        access_log=False,
        # Never let a stuck connection block process shutdown indefinitely.
        timeout_graceful_shutdown=10,
    )
    _Server(config, app.state.theme).run()


class _Server(uvicorn.Server):
    """Ends open theme streams on SIGTERM; uvicorn otherwise waits for them
    (up to the graceful timeout) before the app's shutdown hooks run."""

    def __init__(self, config: uvicorn.Config, theme: OmarchyTheme) -> None:
        super().__init__(config)
        self._theme = theme

    def handle_exit(self, sig, frame) -> None:  # noqa: ANN001 - signal handler signature
        self._theme.close()
        super().handle_exit(sig, frame)


if __name__ == "__main__":  # pragma: no cover
    main()
