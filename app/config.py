"""Typed application configuration loaded from environment variables."""

from functools import lru_cache
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings. Secrets are supplied only through the environment."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", env_prefix="TICKET_", extra="ignore"
    )

    app_name: str = "E-Ticket Platform"
    app_env: str = "development"
    debug: bool = False
    database_url: str = "postgresql+asyncpg://ticket_user:ticket_password@localhost:5432/ticket_platform"
    redis_url: str = "redis://localhost:6379/0"
    jwt_secret: str = "development-only-change-before-production"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = Field(default=30, ge=1, le=24 * 60)
    order_reservation_minutes: int = Field(default=15, ge=1, le=60)
    p2p_withdrawal_percentage_bps: int = Field(default=5000, ge=0, le=10_000)
    p2p_min_withdrawal_paise: int = Field(default=5_000, ge=1)
    p2p_max_withdrawal_paise: int = Field(default=10_000_000, ge=1)
    p2p_threshold_override_enabled: bool = False
    p2p_threshold_paise: int = Field(default=0, ge=0)
    p2p_buyer_payment_window_minutes: int = Field(default=15, ge=1, le=60)
    p2p_receiver_confirmation_window_minutes: int = Field(default=30, ge=1, le=24 * 60)
    p2p_rule_version: str = Field(default="p2p-v1-default", min_length=1, max_length=100)
    p2p_high_risk_maker_checker_required: bool = False
    p2p_require_verified_users: bool = False
    p2p_provider_webhook_secret: str | None = None
    p2p_outbox_batch_size: int = Field(default=100, ge=1, le=1000)
    p2p_expiry_poll_seconds: int = Field(default=60, ge=10, le=3600)
    p2p_outbox_poll_seconds: int = Field(default=15, ge=5, le=3600)
    # Phase 5 payment methods are deliberately disabled by default.  A
    # deployment must configure every trust boundary before exposing a method
    # to buyers; an absent setting never falls back to a test confirmation.
    white_label_provider_namespace: str | None = None
    white_label_webhook_secret: str | None = None
    manual_upi_enabled: bool = False
    telegram_stars_bot_token: str | None = None
    telegram_stars_bot_username: str | None = None
    telegram_stars_webhook_url: str | None = None
    telegram_stars_webhook_secret: str | None = None
    telegram_stars_invoice_payload_secret: str | None = None
    telegram_stars_allow_bot_api_calls: bool = False
    telegram_stars_per_inr: int = Field(default=1, ge=1, le=10_000)
    telegram_stars_price_version: str = Field(default="telegram-stars-v1", min_length=1, max_length=100)
    payment_outbox_batch_size: int = Field(default=100, ge=1, le=1000)
    payment_expiry_poll_seconds: int = Field(default=60, ge=10, le=3600)
    payment_outbox_poll_seconds: int = Field(default=15, ge=5, le=3600)
    payment_reconciliation_poll_seconds: int = Field(default=300, ge=30, le=86_400)
    draw_close_poll_seconds: int = Field(default=60, ge=10, le=3600)
    draw_close_batch_size: int = Field(default=100, ge=1, le=1000)
    allowed_origins: list[str] = Field(default_factory=lambda: ["http://localhost:8000"])

    @field_validator("p2p_provider_webhook_secret", mode="before")
    @classmethod
    def normalize_provider_webhook_secret(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("P2P_PROVIDER_WEBHOOK_SECRET must be a string")
        value = value.strip()
        if not value:
            return None
        if not 16 <= len(value) <= 512:
            raise ValueError("P2P_PROVIDER_WEBHOOK_SECRET must be between 16 and 512 characters")
        return value

    @field_validator("white_label_provider_namespace", mode="before")
    @classmethod
    def normalize_white_label_namespace(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("WHITE_LABEL_PROVIDER_NAMESPACE must be a string")
        value = value.strip()
        if not value:
            return None
        if not 1 <= len(value) <= 64:
            raise ValueError("WHITE_LABEL_PROVIDER_NAMESPACE must be between 1 and 64 characters")
        return value

    @field_validator(
        "white_label_webhook_secret",
        "telegram_stars_bot_token",
        "telegram_stars_webhook_secret",
        "telegram_stars_invoice_payload_secret",
        mode="before",
    )
    @classmethod
    def normalize_phase5_secret(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("payment secrets must be strings")
        value = value.strip()
        if not value:
            return None
        if not 16 <= len(value) <= 512:
            raise ValueError("payment secrets must be between 16 and 512 characters")
        return value

    @field_validator("telegram_stars_bot_username", mode="before")
    @classmethod
    def normalize_telegram_bot_username(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("TELEGRAM_STARS_BOT_USERNAME must be a string")
        value = value.strip()
        if not value:
            return None
        if not 1 <= len(value) <= 128:
            raise ValueError("TELEGRAM_STARS_BOT_USERNAME must be between 1 and 128 characters")
        return value

    @field_validator("telegram_stars_webhook_url", mode="before")
    @classmethod
    def validate_telegram_webhook_url(cls, value: object) -> str | None:
        """Accept only a deployable HTTPS callback URL, never an opaque string."""

        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("TELEGRAM_STARS_WEBHOOK_URL must be a string")
        value = value.strip()
        if not value:
            return None
        if len(value) > 500:
            raise ValueError("TELEGRAM_STARS_WEBHOOK_URL must be at most 500 characters")
        parsed = urlparse(value)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError("TELEGRAM_STARS_WEBHOOK_URL must be an absolute HTTPS URL without credentials")
        return value

    @model_validator(mode="after")
    def reject_unsafe_production_secret(self) -> "Settings":
        if self.app_env.lower() == "production":
            if self.debug:
                raise ValueError("DEBUG must be disabled in production")
            secret = self.jwt_secret.strip()
            if len(secret) < 32 or secret in {
                "development-only-change-before-production",
                "replace-with-a-random-secret-of-at-least-32-characters",
            }:
                raise ValueError("JWT_SECRET must be a non-placeholder secret of at least 32 characters in production")
            database = urlparse(self.database_url)
            if (
                database.scheme != "postgresql+asyncpg"
                or not database.hostname
                or not database.username
                or not database.password
                or database.password == "ticket_password"
            ):
                raise ValueError("DATABASE_URL must use PostgreSQL with non-example credentials in production")
            for origin in self.allowed_origins:
                parsed = urlparse(origin)
                if (
                    parsed.scheme != "https"
                    or not parsed.hostname
                    or "*" in origin
                    or parsed.username is not None
                    or parsed.password is not None
                    or parsed.path not in {"", "/"}
                    or parsed.params
                    or parsed.query
                    or parsed.fragment
                ):
                    raise ValueError("ALLOWED_ORIGINS must contain only exact HTTPS origins in production")
        if self.p2p_max_withdrawal_paise < self.p2p_min_withdrawal_paise:
            raise ValueError("P2P_MAX_WITHDRAWAL_PAISE must be at least P2P_MIN_WITHDRAWAL_PAISE")
        if bool(self.white_label_provider_namespace) != bool(self.white_label_webhook_secret):
            raise ValueError(
                "WHITE_LABEL_PROVIDER_NAMESPACE and WHITE_LABEL_WEBHOOK_SECRET must be configured together"
            )
        telegram_values = (
            self.telegram_stars_bot_token,
            self.telegram_stars_bot_username,
            self.telegram_stars_webhook_url,
            self.telegram_stars_webhook_secret,
            self.telegram_stars_invoice_payload_secret,
        )
        if any(telegram_values) and not all(telegram_values):
            raise ValueError(
                "Telegram Stars bot token, username, webhook URL, and webhook secret must be configured together"
            )
        return self

    @property
    def white_label_enabled(self) -> bool:
        return bool(self.white_label_provider_namespace and self.white_label_webhook_secret)

    @property
    def telegram_stars_enabled(self) -> bool:
        return bool(
            self.telegram_stars_bot_token
            and self.telegram_stars_bot_username
            and self.telegram_stars_webhook_url
            and self.telegram_stars_webhook_secret
            and self.telegram_stars_invoice_payload_secret
            and self.telegram_stars_allow_bot_api_calls
        )


@lru_cache
def get_settings() -> Settings:
    """Return a cached settings instance for the current process."""

    return Settings()
