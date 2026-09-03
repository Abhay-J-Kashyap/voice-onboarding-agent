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

    # SMS delivery. "console" logs the passcode instead of sending it, which is
    # the right default for local work and for a demo with no provider wired up.
    sms_provider: str = "console"
    msg91_auth_key: str = ""
    msg91_sender_id: str = ""
    msg91_template_id: str = ""
    # Tight because the voice platform abandons a tool call after ten seconds.
    # One retry at a three second timeout is a worst case of about 6.5 seconds.
    sms_timeout_seconds: float = 3.0
    sms_max_retries: int = 1

    # Which channel actually carries the passcode. SMS needs DLT registration
    # as a business in India before it will send for real; email does not, so
    # it is worth switching to for a demo run by an individual developer. The
    # default stays "sms" so existing configuration and tests are unaffected —
    # set OTP_DELIVERY_CHANNEL=email explicitly to switch.
    otp_delivery_channel: str = "sms"

    # Email delivery. Same shape as SMS: console logs it, a real provider sends
    # it. Resend's onboarding@resend.dev sandbox sender works with just an API
    # key, no domain verification, but only to the address you signed up with.
    email_provider: str = "console"
    resend_api_key: str = ""
    resend_from_email: str = "Meridian Finance <onboarding@resend.dev>"
    email_timeout_seconds: float = 3.0
    email_max_retries: int = 1

    # Where the emailed application link points. Must be the externally
    # reachable origin, not localhost, or the link in a real email is dead.
    public_base_url: str = "http://localhost:8000"
    #: Long enough that someone can finish after dinner, short enough that a
    #: forwarded or leaked email stops working within a day or two.
    application_link_ttl_hours: int = 48

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
