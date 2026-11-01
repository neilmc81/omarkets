"""Typed configuration for the Omarkets core.

Configuration comes from sensible defaults, an optional local ``.env`` file,
and environment variables prefixed with ``OMARKETS_``. The pre-rename
``MARKETOS_`` prefix is still read for compatibility; when both are set the
``OMARKETS_`` name wins.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from core.config.timezone import system_timezone
from core.desk.identity import LEGACY, PRODUCTION_INSTRUMENTS, Instrument
from core.desk.models import NoteKind
from core.domain.economics import EconomicsMode
from core.services.economics import EconomicPolling
from core.services.news import NewsPolling

OPUS = "anthropic/claude-opus-5.5"
DEEPSEEK = "deepseek/deepseek-v4.1-flash-1017"
#: Strict OpenRouter privacy settings block DeepSeek's own provider, so the
#: request is pinned to these hosts (as in the Oct 2026 blind test).
DEEPSEEK_PROVIDER: dict[str, Any] = {
    "order": ["deepinfra/fp8", "nebius/fp8", "novita/fp8", "parasail/fp8"],
    "quantizations": ["fp8", "bf16", "fp16"],
    "allow_fallbacks": False,
}
#: Weekly review and premarket notes get the strongest model; the rest the cheap one.
DESK_MODEL_DEFAULTS: dict[NoteKind, str] = {
    kind: OPUS if kind in {NoteKind.REVIEW, NoteKind.PREMARKET} else DEEPSEEK
    for kind in NoteKind
}
DESK_MODEL_PARAMS: dict[str, dict[str, Any]] = {DEEPSEEK: {"provider": DEEPSEEK_PROVIDER}}
#: USD per million (input, output) tokens.
DESK_MODEL_RATES: dict[str, tuple[float, float]] = {OPUS: (5.0, 25.0), DEEPSEEK: (0.15, 0.60)}


#: Pre-rename environment prefix, still accepted for compatibility.
LEGACY_ENV_PREFIX = "MARKETOS_"


class Settings(BaseSettings):
    """Runtime configuration for the Omarkets core."""

    model_config = SettingsConfigDict(
        env_prefix="OMARKETS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Old MARKETOS_* names (environment, then .env) come after every new
        # OMARKETS_* source, so the new name wins whenever both are set.
        legacy = LEGACY_ENV_PREFIX
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            EnvSettingsSource(settings_cls, env_prefix=legacy),
            DotEnvSettingsSource(
                settings_cls,
                # Honour a per-call ``_env_file`` (or ``_env_file=None``).
                env_file=getattr(dotenv_settings, "env_file", ".env"),
                env_prefix=legacy,
            ),
            file_secret_settings,
        )

    # --- API ---
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8765, ge=1, le=65535)
    log_level: str = "INFO"
    # Zero disables that spending limit; both zero also remove request-count caps.
    ai_monthly_budget_usd: float = Field(default=5.0, ge=0, allow_inf_nan=False)
    ai_daily_budget_usd: float = Field(default=0.20, ge=0, allow_inf_nan=False)
    ai_weekly_budget_usd: float = Field(default=0, ge=0, allow_inf_nan=False)
    #: Where budget days and weeks start. Defaults to the system time zone.
    ai_budget_timezone: str = Field(default_factory=system_timezone)
    ai_openrouter_daily_request_limit: int | None = Field(default=None, ge=0)
    ai_openrouter_background_request_limit: int | None = Field(default=None, ge=0)
    ai_budget_db_path: Path | None = None

    @field_validator("ai_budget_timezone", "desk_local_timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value

    @property
    def resolved_ai_budget_db_path(self) -> Path:
        return self.ai_budget_db_path or self.data_dir / "ai-cost-control.sqlite3"

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ValueError(f"log_level must be a standard level, got {value!r}")
        return level

    @field_validator("market_data_mode")
    @classmethod
    def _validate_market_mode(cls, value: str) -> str:
        mode = value.strip().lower()
        if mode not in {"live", "demo"}:
            raise ValueError("market_data_mode must be 'live' or 'demo'")
        return mode

    # --- Storage ---
    data_dir: Path = Path("./data")
    # Explicit database path. When unset it resolves to a per-mode file so a
    # demo run can never write synthetic observations into the live database.
    duckdb_path: Path | None = None

    economic_mode: EconomicsMode = EconomicsMode.LIVE
    economic_db_path: Path | None = None
    economic_poll_seconds: float = Field(default=21600, ge=30, allow_inf_nan=False)
    economic_poll_overrides: dict[str, float] = Field(default_factory=dict)
    economic_transition_seconds: float = Field(default=30, ge=1, allow_inf_nan=False)
    economic_proximity_seconds: float = Field(default=300, ge=30, allow_inf_nan=False)
    economic_proximity_before_seconds: float = Field(default=1800, ge=0, allow_inf_nan=False)
    economic_proximity_after_seconds: float = Field(default=1800, ge=0, allow_inf_nan=False)
    fred_api_key: SecretStr | None = None
    bls_api_key: SecretStr | None = None
    census_api_key: SecretStr | None = None
    bea_api_key: SecretStr | None = None
    eia_api_key: SecretStr | None = None

    @field_validator("api_host")
    @classmethod
    def _loopback_only(cls, value: str) -> str:
        if value != "127.0.0.1":
            raise ValueError("local API must bind to 127.0.0.1")
        return value

    @field_validator("economic_poll_overrides")
    @classmethod
    def _economic_intervals(cls, value: dict[str, float]) -> dict[str, float]:
        policy = EconomicPolling(overrides=value)
        for name in value:
            policy.interval(name)
        return value

    @property
    def resolved_economic_db_path(self) -> Path:
        return (
            self.economic_db_path or self.data_dir / f"economics-{self.economic_mode.value}.sqlite3"
        )

    # --- Provider ---
    # Market data mode: "live" uses real external feeds; "demo" uses the
    # deterministic synthetic provider. Demo is never an automatic fallback.
    market_data_mode: str = "live"
    # Explicit adapter override. Empty selects the adapter for market_data_mode.
    data_provider: str = ""
    provider_update_interval: float = Field(default=2.0, gt=0)
    provider_seed: int | None = 7
    market_poll_seconds: float = Field(default=5.0, ge=1, allow_inf_nan=False)
    futures_roll_days: int = Field(default=8, ge=0)
    market_live_lag_seconds: float = Field(default=60.0, ge=0, allow_inf_nan=False)
    # Live-feed freshness thresholds per asset class. Real free feeds update at
    # seconds-to-minutes cadence and can be quiet overnight, so the Phase 1
    # synthetic thresholds (seconds) would mislabel healthy data as stale.
    market_stale_overrides: dict[str, float] = Field(
        default_factory=lambda: {
            "equity_index_future": 900.0,
            "equity": 900.0,
            "treasury_yield": 300.0,
            "fx_index": 300.0,
            "volatility_index": 900.0,
            "commodity": 300.0,
            "crypto": 120.0,
        }
    )

    # --- Staleness ---
    # Default freshness threshold in seconds when no per-asset-class override
    # applies. Data older than its threshold is reported as "stale".
    stale_default_seconds: float = Field(default=30.0, gt=0)
    # Per-asset-class overrides, e.g.
    # OMARKETS_STALE_OVERRIDES='{"equity_index_future": 5, "crypto": 10}'
    stale_overrides: dict[str, float] = Field(default_factory=dict)

    # --- News / geopolitical event ingestion (Phase 5) ---
    # Deterministic event ingestion is on by default. It requires no credentials
    # for the built-in public/official feeds.
    news_enabled: bool = True
    news_db_path: Path | None = None
    # Default poll interval in seconds for sources without a catalog override.
    news_poll_seconds: float = Field(default=300, ge=30, allow_inf_nan=False)
    # Per-source poll intervals, e.g. OMARKETS_NEWS_POLL_OVERRIDES='{"cnbc_top": 60}'
    news_poll_overrides: dict[str, float] = Field(default_factory=dict)
    # Explicit source allow-list. Empty means "all enabled catalog sources".
    news_enabled_sources: list[str] = Field(default_factory=list)
    news_disabled_sources: list[str] = Field(default_factory=list)
    # Near-duplicate Jaccard threshold (0-1) and merge window in hours. The
    # default matches core.ingest.dedupe.DEFAULT_SIMILARITY_THRESHOLD.
    news_dedupe_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    news_dedupe_window_hours: float = Field(default=36.0, gt=0, allow_inf_nan=False)

    @field_validator("news_poll_overrides")
    @classmethod
    def _news_intervals(cls, value: dict[str, float]) -> dict[str, float]:
        policy = NewsPolling(overrides=value)
        for name in value:
            policy.interval(name)
        return value

    @property
    def resolved_news_db_path(self) -> Path:
        return self.news_db_path or self.data_dir / "events.sqlite3"

    openrouter_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "OPENROUTER_API_KEY", "OMARKETS_OPENROUTER_API_KEY", "MARKETOS_OPENROUTER_API_KEY"
        ),
    )

    # --- Desk: plain-English notes for the Today screen ---
    desk_enabled: bool = True
    #: OpenRouter model per note kind. Which model writes a note, first match wins:
    #: 1. ``desk_models[kind]`` (``OMARKETS_DESK_MODELS='{"move": "..."}'``);
    #: 2. ``desk_model`` (``OMARKETS_DESK_MODEL``), the old single model, for every kind;
    #: 3. ``DESK_MODEL_DEFAULTS``.
    desk_models: dict[NoteKind, str] = Field(default_factory=dict)
    desk_model: str | None = None
    #: Writes the note once more when the first model fails (HTTP, invalid
    #: output, advice filter). Empty disables the retry.
    desk_fallback_model: str = OPUS
    #: Extra OpenRouter request fields per model, merged into the request body.
    #: Entries replace ``DESK_MODEL_PARAMS`` per model; ``{}`` drops a default.
    desk_model_params: dict[str, dict[str, Any]] = Field(default_factory=dict)
    #: Upper-bound USD per million (input, output) tokens per model, used to
    #: reserve budget before each call. Set at or above the highest provider price.
    #: Entries replace ``DESK_MODEL_RATES`` per model.
    desk_model_rates: dict[str, tuple[float, float]] = Field(default_factory=dict)
    #: Rates for any model in neither.
    desk_input_rate: float = Field(default=3.0, gt=0, allow_inf_nan=False)
    desk_output_rate: float = Field(default=15.0, gt=0, allow_inf_nan=False)
    desk_db_path: Path | None = None
    #: Prompt version new notes are written with (``core/desk/prompts.py``).
    #: desk:v8 (NQ's text only) can still be selected for an NQ-only desk.
    desk_prompt_version: Literal["desk:v8", "desk:v9"] = "desk:v9"
    #: Instruments that get their own notes (default NQ, ES and YM), e.g.
    #: ``OMARKETS_DESK_INSTRUMENTS='["NQ"]'``. NQ gets pre-market, move and
    #: ask notes; every other instrument move and ask only. NQ is always on.
    #: Each needs a prompt profile at ``desk_prompt_version`` (ES, YM: desk:v9), or
    #: the service refuses to start (``core.main.check_desk_instruments``).
    desk_instruments: list[str] = Field(default_factory=lambda: list(PRODUCTION_INSTRUMENTS))
    #: NQ move over about an hour that earns its own "why is NQ moving" note, used
    #: until there is enough history to scale it (``core/analysis/move_size.py``).
    desk_move_threshold_pct: float = Field(default=0.8, gt=0, allow_inf_nan=False)
    desk_notify: bool = True
    #: The 07:00 ET "what's on today" notification (no AI).
    desk_daily_plan: bool = True
    #: The reader's time zone; the daily plan shows times in it. Defaults to
    #: the system time zone.
    desk_local_timezone: str = Field(default_factory=system_timezone)
    #: Active Omarchy theme directory the UI takes its palette from. Defaults
    #: to ``$XDG_STATE_HOME/omarchy/current/theme``.
    omarchy_theme_dir: Path | None = None

    @field_validator("desk_instruments")
    @classmethod
    def _desk_instruments(cls, value: list[str]) -> list[str]:
        symbols = list(dict.fromkeys(value))
        for symbol in symbols:
            Instrument.of(symbol)  # ValueError names a malformed symbol
        if LEGACY not in symbols:
            raise ValueError(f"{LEGACY} is the desk's own instrument and cannot be left out")
        return symbols

    @property
    def resolved_desk_db_path(self) -> Path:
        return self.desk_db_path or self.data_dir / "desk.sqlite3"

    #: The Chart focus and the watchlist (``core/storage/workspace.py``).
    workspace_db_path: Path | None = None

    @property
    def resolved_workspace_db_path(self) -> Path:
        return self.workspace_db_path or self.data_dir / "workspace.sqlite3"

    # --- Native Omarchy integration (Phase 8) ---
    # Serve the built web workstation from the backend so the native layer and
    # the launcher need one process, not a separate Vite preview server. The
    # directory is the Vite build output (web/dist); disable to run the frontend
    # separately in development.
    serve_frontend: bool = True
    frontend_dist: Path | None = None

    @property
    def resolved_frontend_dist(self) -> Path:
        return self.frontend_dist or Path(__file__).resolve().parents[2] / "web" / "dist"

    def desk_model_for(self, kind: NoteKind) -> str:
        return self.desk_models.get(kind) or self.desk_model or DESK_MODEL_DEFAULTS[kind]

    @property
    def resolved_desk_model_params(self) -> dict[str, dict[str, Any]]:
        return {**DESK_MODEL_PARAMS, **self.desk_model_params}

    @property
    def resolved_desk_model_rates(self) -> dict[str, tuple[float, float]]:
        return {**DESK_MODEL_RATES, **self.desk_model_rates}

    @model_validator(mode="after")
    def _resolve_database_path(self) -> Settings:
        if self.duckdb_path is None:
            # Namespace per market-data mode. Synthetic demo observations must
            # never share a database with real live observations: demo values can
            # carry a newer event timestamp than a real cash-market quote and
            # would then win the latest-wins comparison forever.
            self.duckdb_path = self.default_duckdb_path
        return self

    @property
    def default_duckdb_path(self) -> Path:
        """The per-mode database used when ``duckdb_path`` is not set.

        New installs get ``omarkets-<mode>.duckdb``. An install from before the
        rename keeps reading its ``marketos-<mode>.duckdb`` until a new file exists.
        """
        new = self.data_dir / f"omarkets-{self.market_data_mode}.duckdb"
        old = self.data_dir / f"marketos-{self.market_data_mode}.duckdb"
        return old if old.exists() and not new.exists() else new

    @property
    def legacy_duckdb_path(self) -> Path:
        """The pre-namespacing single market database, if one exists.

        Used only for a one-time migration of real observations into the live
        database. An explicit ``duckdb_path`` is never migrated.
        """
        return self.data_dir / "marketos.duckdb"

    @property
    def resolved_duckdb_path(self) -> Path:
        """The database path, guaranteed non-null after validation."""
        path = self.duckdb_path
        if path is None:  # pragma: no cover - guaranteed by _resolve_database_path
            raise RuntimeError("duckdb_path was not resolved by the model validator")
        return path
