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

    # One-time passcode policy. All enforced in the service; the prompt only
    # describes them.
    otp_length: int = 6
    otp_ttl_seconds: int = 300
    otp_max_verify_attempts: int = 3
    otp_max_resends: int = 2
    #: Ceiling on codes issued to one customer inside the window below, so a
    #: caller cannot be used to spam a stranger's phone.
    otp_max_per_window: int = 3
    otp_rate_window_seconds: int = 900

    # Returns the generated passcode in the tool response under `data.demo_otp`
    # so a live demo can be driven without an SMS provider. The value is never
    # placed in `agent_message`, so the model cannot read it aloud. MUST be
    # false in any real deployment.
    otp_demo_mode: bool = False

    # Log level for the structured JSON logger.
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
