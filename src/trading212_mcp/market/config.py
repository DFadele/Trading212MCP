from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class MarketSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MARKET_",
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,
    )
    fmp_api_key: SecretStr | None = None
    provider: Literal["fmp", "yfinance"] = "yfinance"
    yahoo_isin_map: dict[str, str] = Field(default_factory=dict[str, str])
    yahoo_regions: list[str] = Field(default_factory=lambda: ["us"])
    fundamentals_source: Literal["provider", "sec", "fmp"] = "sec"
    sec_user_agent: str | None = None
    requests_per_minute: int = Field(default=60, ge=1, le=3000)
    cache_path: Path | None = None
    cache_ttl_seconds: int = Field(default=3600, ge=0, le=86400)
    cache_max_entries: int = Field(default=512, ge=1, le=10000)
    timeout_seconds: float = Field(default=20, gt=0, le=60)
    fetch_concurrency: int = Field(default=4, ge=1, le=8)
    risk_timeout_seconds: float = Field(default=50, gt=0, le=600)
    retries: int = Field(default=2, ge=0, le=3)
    min_observations: int = Field(default=60, ge=3, le=252)
    screen_max_candidates: int = Field(default=40, ge=1, le=200)
    instrument_overrides: dict[str, str] = Field(default_factory=dict[str, str])
    openfigi_enabled: bool = True
    openfigi_api_key: SecretStr | None = None
    resolution_cache_path: Path = Path(".cache/instrument-mappings.sqlite3")
    resolution_cache_ttl_days: int = Field(default=30, ge=1, le=365)
