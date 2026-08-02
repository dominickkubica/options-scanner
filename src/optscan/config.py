"""Application settings.

Everything tunable lives here or in a YAML config file loaded from here. No module
outside this one reads os.environ directly, and no module hardcodes a threshold.
"""

from __future__ import annotations

from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]

HOURS_PER_DAY = 24
MINUTES_PER_HOUR = 60
MINUTES_PER_DAY = HOURS_PER_DAY * MINUTES_PER_HOUR

ProviderName = Literal["yfinance", "schwab", "tradier"]
LogFormat = Literal["console", "json"]
TradierEnvironment = Literal["sandbox", "production"]

#: Tradier's documented hosts, verified against docs.tradier.com on 2026-07-31.
TRADIER_HOSTS: dict[str, str] = {
    "sandbox": "https://sandbox.tradier.com",
    "production": "https://api.tradier.com",
}

#: Tradier delays every sandbox response by this much. Their FAQ: "We delay our market
#: data the industry standard 15-minutes for all sandbox data." It is a documented
#: property of the tier, not something the responses announce, so it is recorded here
#: and shown in the UI rather than inferred from a timestamp.
SANDBOX_DELAY_MINUTES = 15


class Settings(BaseSettings):
    """Runtime configuration, populated from environment variables and .env."""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="OPTSCAN_",
        extra="ignore",
    )

    env: Literal["dev", "prod"] = "dev"

    # Logging
    log_level: str = "INFO"
    log_format: LogFormat = "console"

    # Storage. Paths are resolved relative to the repo root when given as relative.
    data_dir: Path = Path("data")
    snapshot_dir_name: str = "snapshots"
    sqlite_filename: str = "optscan.sqlite"

    # Market data
    provider: ProviderName = "yfinance"
    request_timeout_seconds: float = 10.0
    max_retries: int = 3
    retry_backoff_seconds: float = 1.5

    # Pricing assumptions. Vendor greeks hide these; ours are explicit and logged.
    risk_free_rate: float = Field(default=0.043, ge=0.0, le=0.25)
    trading_days_per_year: int = Field(default=252, gt=0)

    # Market calendar. Everything time related resolves through this.
    market_calendar: str = "NYSE"
    market_timezone: str = "America/New_York"

    # Daily snapshot job. The capture time is the load bearing choice here: IV rank
    # compares today against history, so history has to be sampled at a consistent
    # time of day. 15:45 local is late enough to be a real mark and early enough to
    # avoid the closing auction, when quotes widen and the tape gets noisy.
    snapshot_time_local: str = "15:45"
    snapshot_max_dte: int = Field(default=400, gt=0)
    snapshot_max_expiries: int = Field(default=16, gt=0)
    snapshot_history_days: int = Field(default=400, gt=0)

    # Recording scores the latest stored snapshot, so it has to run after the capture
    # rather than alongside it. Early enough to still be inside the session, because a
    # scan that runs after the close is scoring a chain nobody can trade.
    record_delay_minutes: int = Field(default=30, ge=0, lt=MINUTES_PER_DAY)

    # Settling runs the next morning rather than after the close. It only needs the
    # underlying's close on expiry day, and asking for that on expiry day itself asks
    # for a bar the vendor has not published. Before the open, every expiry it can see
    # is strictly finished.
    resolve_time_local: str = "08:00"

    # Position management. Off the schedule by default: see the manage job's entry in
    # jobs/schedule.py for why polling a free vendor every quarter hour is a risk to
    # the one job whose history cannot be rebuilt.
    manage_interval_minutes: int = Field(default=15, gt=0)

    # Backups. None means "beside the repo but not inside it", resolved in backup_path.
    # Inside data/ would be worthless: the point is surviving the loss of data/.
    backup_dir: Path | None = None

    # How many dated database copies to keep. The parquet mirror is never rotated,
    # because deleting from it would delete the history it exists to protect.
    backup_keep: int = Field(default=30, gt=0)

    # Default watchlist, seeded into sqlite the first time the database is created.
    # After that the database is the source of truth and this is ignored.
    # NoDecode because pydantic-settings would otherwise try to JSON parse the env var,
    # and SPY,QQQ is what a person actually writes in a .env file.
    default_watchlist: Annotated[tuple[str, ...], NoDecode] = (
        "SPY",
        "QQQ",
        "IWM",
        "AAPL",
        "MSFT",
        "NVDA",
    )

    # Credentials. Never logged, never committed. Optional until Phase 5.
    tradier_token: SecretStr | None = None
    schwab_client_id: SecretStr | None = None
    schwab_client_secret: SecretStr | None = None

    # Tradier. The environment decides the host and, more importantly, whether the
    # data can be called real time at all: sandbox is documented as 15 minutes delayed.
    tradier_environment: TradierEnvironment = "sandbox"

    # Whether the production account actually carries a real time market data
    # entitlement. Nothing in a Tradier response says so, and a quote that is silently
    # delayed looks exactly like one that is not, so this is asked rather than guessed
    # and it defaults to the answer that cannot mislead. Ignored in sandbox, which is
    # delayed regardless of what this says.
    tradier_realtime_entitled: bool = False

    # Documented market data limit: 120 requests per minute in production, 60 in
    # sandbox, enforced per minute per access token. The default is the lower one
    # because exceeding it is worse than being slower than necessary.
    tradier_requests_per_minute: int = Field(default=60, gt=0)

    # Live feed. Off by default: Phases 0 to 4 are a stored snapshot tool and must keep
    # behaving like one until somebody asks for live data.
    live_enabled: bool = False

    # How often a subscribed symbol is refetched while the market is open. Two requests
    # per cycle per symbol (one quote, one chain), so the default costs 8 of the 60
    # sandbox requests a minute for one symbol.
    live_poll_seconds: float = Field(default=15.0, ge=1.0)

    # Multiplier applied to the poll interval outside the regular session. Pre and post
    # market chains barely move and the budget is better spent when it matters. Nothing
    # is polled at all when the market is closed.
    live_offhours_poll_multiple: float = Field(default=8.0, ge=1.0)

    # How long a fetched live chain is reused when several subscribers want the same
    # symbol and expiry. Short: this is deduplication, not caching.
    live_chain_ttl_seconds: float = Field(default=5.0, ge=0.0)

    # Ceiling on symbols polled at once, so an open browser tab cannot spend the whole
    # request budget by cycling through the watchlist.
    live_max_symbols: int = Field(default=4, gt=0)

    # A symbol stops being polled this long after its last subscriber disconnects.
    live_idle_timeout_seconds: float = Field(default=60.0, gt=0.0)

    # API server
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        level = value.upper()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if level not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}, got {value!r}")
        return level

    @field_validator("snapshot_time_local", "resolve_time_local")
    @classmethod
    def _valid_clock_time(cls, value: str, info: ValidationInfo) -> str:
        """Parsed here so a typo fails at startup, not at 15:45 with nobody watching."""
        name = info.field_name
        try:
            hour, minute = (int(part) for part in value.split(":", 1))
        except ValueError as exc:
            raise ValueError(f"{name} must look like HH:MM, got {value!r}") from exc
        if not (0 <= hour < HOURS_PER_DAY and 0 <= minute < MINUTES_PER_HOUR):
            raise ValueError(f"{name} is not a real time of day: {value!r}")
        return f"{hour:02d}:{minute:02d}"

    @field_validator("tradier_token", "schwab_client_id", "schwab_client_secret", mode="before")
    @classmethod
    def _blank_secret_is_unset(cls, value: object) -> object:
        """An empty .env entry means unset, not set to the empty string.

        .env.example ships these keys blank, so without this every fresh install
        reports three configured credentials and Phase 5 would fail authentication
        while the startup log insists the token is there.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("default_watchlist", mode="before")
    @classmethod
    def _split_watchlist(cls, value: object) -> object:
        """Accept a comma separated string so the .env form stays readable."""
        if isinstance(value, str):
            return tuple(part.strip().upper() for part in value.split(",") if part.strip())
        return value

    @property
    def snapshot_time(self) -> time:
        """Configured capture time as a time object, in market local time."""
        hour, minute = (int(part) for part in self.snapshot_time_local.split(":", 1))
        return time(hour=hour, minute=minute)

    @property
    def record_time(self) -> time:
        """When the recording job runs, in market local time.

        Derived from the capture time rather than configured on its own, because the
        two are not independent: recording scores the snapshot, so moving the capture
        without moving this would silently start scoring yesterday's chain.
        """
        minutes = self.snapshot_time.hour * MINUTES_PER_HOUR + self.snapshot_time.minute
        shifted = (minutes + self.record_delay_minutes) % MINUTES_PER_DAY
        return time(hour=shifted // MINUTES_PER_HOUR, minute=shifted % MINUTES_PER_HOUR)

    @property
    def resolve_time(self) -> time:
        """When the settlement job runs, in market local time."""
        hour, minute = (int(part) for part in self.resolve_time_local.split(":", 1))
        return time(hour=hour, minute=minute)

    @property
    def tradier_base_url(self) -> str:
        """Host for the configured Tradier environment, without a trailing slash."""
        return TRADIER_HOSTS[self.tradier_environment]

    @property
    def tradier_is_realtime(self) -> bool:
        """Whether Tradier quotes on this configuration may be called real time.

        Sandbox never can: it is documented as 15 minutes delayed and there is no
        setting that changes it. Production only can when the account holder has said
        their entitlement is real time, because the responses do not carry that fact.
        """
        return self.tradier_environment == "production" and self.tradier_realtime_entitled

    @property
    def quote_delay_minutes(self) -> int | None:
        """Known delay on the configured provider's quotes, when it is documented.

        None means the delay is unknown rather than zero. yfinance is delayed by
        roughly fifteen minutes but does not document it, and reporting an
        undocumented number next to a documented one would give both the same weight.
        """
        if self.provider == "tradier" and self.tradier_environment == "sandbox":
            return SANDBOX_DELAY_MINUTES
        return None

    @property
    def data_path(self) -> Path:
        """Absolute data directory."""
        return self.data_dir if self.data_dir.is_absolute() else REPO_ROOT / self.data_dir

    @property
    def snapshot_path(self) -> Path:
        """Absolute directory for parquet chain snapshots."""
        return self.data_path / self.snapshot_dir_name

    @property
    def sqlite_path(self) -> Path:
        """Absolute path to the sqlite database file."""
        return self.data_path / self.sqlite_filename

    @property
    def backup_path(self) -> Path:
        """Absolute backup directory.

        Defaults beside the repository rather than inside it. A backup under `data/`
        would be destroyed by everything that destroys `data/`, which is every failure
        this exists for. Point it at another drive or a synced folder to get a copy
        that survives losing this one: `backup_path_is_same_volume` reports whether it
        currently does.
        """
        if self.backup_dir is None:
            return REPO_ROOT.parent / "optscan-backups"
        return self.backup_dir if self.backup_dir.is_absolute() else REPO_ROOT / self.backup_dir

    @property
    def backup_path_is_same_volume(self) -> bool:
        """Whether the backup lands on the same drive as the data it copies.

        Said out loud because a same drive copy protects against the mistakes, which
        are common, and not against the drive, which is the reason people run backups.
        """
        return self.backup_path.anchor.lower() == self.data_path.anchor.lower()

    def ensure_dirs(self) -> None:
        """Create the data directories this process writes to."""
        self.snapshot_path.mkdir(parents=True, exist_ok=True)

    def safe_summary(self) -> dict[str, object]:
        """Config values that are safe to log. Secrets are reported as set or unset only."""
        return {
            "env": self.env,
            "provider": self.provider,
            "log_level": self.log_level,
            "log_format": self.log_format,
            "data_path": str(self.data_path),
            "risk_free_rate": self.risk_free_rate,
            "tradier_environment": self.tradier_environment,
            "live_enabled": self.live_enabled,
            "credentials_set": sorted(
                name
                for name in ("tradier_token", "schwab_client_id", "schwab_client_secret")
                if getattr(self, name) is not None
            ),
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton. Call get_settings.cache_clear() in tests."""
    return Settings()
