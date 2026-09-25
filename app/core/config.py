from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All settings come from environment variables or a local `.env` file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    redis_url: str = "redis://127.0.0.1:6379/0"
    key_prefix: str = Field(default="taskq:", pattern=r"^[A-Za-z0-9_:\-]{1,64}$")

    job_ttl_seconds: int = Field(default=86_400, ge=60)
    dead_letter_ttl_seconds: int = Field(default=604_800, ge=60)
    max_payload_bytes: int = Field(default=16_384, ge=256, le=1_048_576)
    max_result_bytes: int = Field(default=16_384, ge=256, le=1_048_576)
    max_error_chars: int = Field(default=500, ge=50, le=5_000)

    default_max_attempts: int = Field(default=3, ge=1, le=10)
    retry_base_seconds: float = Field(default=1.0, gt=0)
    retry_max_seconds: float = Field(default=60.0, gt=0)

    lease_seconds: float = Field(default=30.0, gt=0)
    heartbeat_seconds: float = Field(default=5.0, gt=0)
    worker_stale_seconds: float = Field(default=15.0, gt=0)
    idle_wait_seconds: float = Field(default=1.0, gt=0)
    maintenance_seconds: float = Field(default=0.5, gt=0)
    shutdown_grace_seconds: float = Field(default=20.0, ge=0)

    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65_535)
    # Host names the API answers to. Others are refused, which blocks DNS rebinding.
    allowed_hosts: list[str] = ["127.0.0.1", "localhost"]
    max_body_bytes: int = Field(default=131_072, ge=1_024, le=8_388_608)
    list_scan_limit: int = Field(default=2_000, ge=100, le=100_000)
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
