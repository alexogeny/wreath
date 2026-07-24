"""Runtime configuration via pydantic-settings (BaseSettings)."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class TrailSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TUMBLEWEED_", extra="ignore")

    database_url: str = "postgresql://localhost/tumbleweed"
    read_replica_url: str = "postgresql://localhost/tumbleweed_ro"
    herd_radio_url: str = "amqp://guest:guest@localhost:5672/"
    oidc_issuer: str = "https://auth.tumbleweed.example/"
    oidc_audience: str = "tumbleweed-api"
    m2m_client_id: str = "trailhead-service"
    m2m_client_secret: str = "change-me"
    environment: str = "production"


@lru_cache
def get_settings() -> TrailSettings:
    return TrailSettings()
