"""Runtime configuration. Loaded once at process start from ``.env`` plus
the process environment. Never mutated after load."""

from functools import cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class HuragokSettings(BaseSettings):
    """Typed view over environment variables and the repo's ``.env`` file.

    Most settings take the ``HURAGOK_`` prefix; three external tokens
    (Anthropic and Telegram) are read from their conventional unprefixed
    names so operators can reuse existing env files without translation.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="HURAGOK_",
        case_sensitive=False,
        extra="ignore",
    )

    # Secrets are read from their conventional unprefixed env var names.
    # Optional in Slice A so the CLI can run against fixtures without
    # provisioning any of the external integrations.
    anthropic_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="ANTHROPIC_API_KEY",
    )
    anthropic_admin_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="ANTHROPIC_ADMIN_API_KEY",
    )
    telegram_bot_token: SecretStr | None = Field(
        default=None,
        validation_alias="TELEGRAM_BOT_TOKEN",
    )

    # Prefixed settings (``HURAGOK_*``).
    telegram_default_chat_id: str | None = None
    log_level: str = "info"
    data_dir: str | None = None


@cache
def load_settings() -> HuragokSettings:
    """Load settings from the environment; cached so callers share one instance."""
    return HuragokSettings()
