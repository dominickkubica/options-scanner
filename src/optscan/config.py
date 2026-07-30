"""Application settings.

Everything tunable lives here or in a YAML config file loaded from here. No module
outside this one reads os.environ directly, and no module hardcodes a threshold.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]

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
