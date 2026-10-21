"""Application settings, read from environment variables or a `.env` file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All tunable knobs in one place. Every field can be overridden by an env var
    with the same name in upper case (e.g. ``MAX_ROWS=100``)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # AI provider
    ai_provider: Literal["gemini", "fake"] = "gemini"
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-flash-lite-latest"
    request_timeout_seconds: float = 60.0

    # Upload limits
    max_upload_bytes: int = Field(default=2 * 1024 * 1024, gt=0)
    max_rows: int = Field(default=200, gt=0)
    max_review_chars: int = Field(default=1500, gt=0)

    # Abuse / cost protection
    rate_limit_per_ip: int = Field(default=5, gt=0)
    rate_limit_window_seconds: int = Field(default=3600, gt=0)
    daily_api_call_limit: int = Field(default=300, ge=0)
    max_concurrent_jobs: int = Field(default=3, gt=0)

    # Batching and retries
    batch_size: int = Field(default=25, gt=0, le=100)
    batch_concurrency: int = Field(default=2, gt=0)
    max_retries: int = Field(default=4, ge=1)
    retry_base_delay_seconds: float = 1.5

    # Finished jobs are kept in memory this long so results can be downloaded.
    job_ttl_seconds: int = 3600

    data_dir: Path = Path("./data")


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings (cached so .env is read once)."""
    return Settings()
