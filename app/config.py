"""Application configuration.

All runtime configuration is read from the environment so the same image can be
promoted across environments without a rebuild.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "kyc-onboarding-tool-service"
    environment: str = "local"

    # SQLite by default so the service runs with zero infrastructure.
    # Point at Postgres in production: postgresql+psycopg://user:pass@host/db
    database_url: str = "sqlite:///./kyc_agent.db"

    # Shared secret presented by the voice platform on every tool call.
    api_key: str = "local-dev-key"

    # Policy limits enforced server side, never left to the model's discretion.
    max_identity_attempts: int = 2
    eligibility_policy_version: str = "v1.0.0"

    # Log level for the structured JSON logger.
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
