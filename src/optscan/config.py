"""Application settings.

Everything tunable lives here or in a YAML config file loaded from here. No module
outside this one reads os.environ directly, and no module hardcodes a threshold.
"""

from __future__ import annotations

from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]

HOURS_PER_DAY = 24
MINUTES_PER_HOUR = 60

ProviderName = Literal["yfinance", "schwab", "tradier"]
LogFormat = Literal["console", "json"]


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

    @field_validator("snapshot_time_local")
    @classmethod
    def _valid_clock_time(cls, value: str) -> str:
        """Parsed here so a typo fails at startup, not at 15:45 with nobody watching."""
        try:
            hour, minute = (int(part) for part in value.split(":", 1))
        except ValueError as exc:
            raise ValueError(f"snapshot_time_local must look like HH:MM, got {value!r}") from exc
        if not (0 <= hour < HOURS_PER_DAY and 0 <= minute < MINUTES_PER_HOUR):
            raise ValueError(f"snapshot_time_local is not a real time of day: {value!r}")
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
