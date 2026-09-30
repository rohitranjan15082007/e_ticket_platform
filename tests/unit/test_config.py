"""Configuration trust-boundary tests."""

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.config import Settings


def test_telegram_webhook_url_requires_absolute_https_without_credentials() -> None:
    values = {
        "telegram_stars_bot_token": "telegram-test-token-1234",
        "telegram_stars_bot_username": "phase5_config_test_bot",
        "telegram_stars_webhook_secret": "telegram-webhook-secret-1234",
        "telegram_stars_invoice_payload_secret": "telegram-payload-secret-1234",
    }

    with pytest.raises(PydanticValidationError, match="absolute HTTPS URL"):
        Settings(telegram_stars_webhook_url="http://example.test/webhooks/telegram", **values)
    with pytest.raises(PydanticValidationError, match="without credentials"):
        Settings(telegram_stars_webhook_url="https://user:pass@example.test/webhooks/telegram", **values)

    configured = Settings(telegram_stars_webhook_url="https://example.test/webhooks/telegram", **values)
    assert configured.telegram_stars_webhook_url == "https://example.test/webhooks/telegram"
