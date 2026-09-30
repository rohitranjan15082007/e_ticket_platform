"""Public Phase 5 payment-webhook trust-boundary coverage.

These tests exercise the HTTP routers rather than calling the orchestrator
directly.  In particular, they ensure the exact raw white-label request is
signed before it is retained and that Telegram pre-checkout updates are
authenticated and acknowledged without using the real Bot API.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hmac
from hashlib import sha256
import json
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_session
from app.main import app
from app.models.order import DeliveryStatus, Order, OrderStatus
from app.models.payment import (
    PaymentAttempt,
    PaymentAttemptStatus,
    PaymentMethod,
    PaymentProviderEvent,
    PaymentSettlement,
)
from app.models.user import IdempotencyRecord, User
from app.payment_adapters.telegram_stars_adapter import TelegramStarsAdapter
from app.services.payment_orchestrator import PaymentOrchestrator


async def _user(session: AsyncSession, *, label: str) -> User:
    user = User(
        email=f"phase5-webhook-{label}-{uuid4()}@example.test",
        password_hash="test-only-password-hash",
    )
    session.add(user)
    await session.commit()
    return user


async def _order(session: AsyncSession, *, buyer: User, amount_paise: int) -> Order:
    order = Order(
        buyer_user_id=buyer.id,
        status=OrderStatus.PENDING_PAYMENT,
        delivery_status=DeliveryStatus.NOT_STARTED,
        total_paise=amount_paise,
        currency="INR",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
    )
    session.add(order)
    await session.commit()
    return order


async def _white_label_attempt(
    session: AsyncSession, *, buyer: User, namespace: str, amount_paise: int
) -> tuple[Order, PaymentAttempt]:
    """Seed a provider-owned checkout because generic initiation is intentionally unavailable."""

    order = await _order(session, buyer=buyer, amount_paise=amount_paise)
    order.status = OrderStatus.AWAITING_PAYMENT
    attempt_id = uuid4()
    record = IdempotencyRecord(
        id=uuid4(),
        actor_scope=f"test:phase5:white-label:{buyer.id}",
        idempotency_key=f"phase5-webhook-white-label-{uuid4()}",
        request_fingerprint="a" * 64,
        resource_type="payment_attempt",
        resource_id=attempt_id,
        status_code=201,
    )
    attempt = PaymentAttempt(
        id=attempt_id,
        order_id=order.id,
        buyer_user_id=buyer.id,
        method=PaymentMethod.WHITE_LABEL,
        provider_namespace=namespace,
        status=PaymentAttemptStatus.AWAITING_PAYMENT,
        order_amount_paise=amount_paise,
        order_currency="INR",
        provider_amount=amount_paise,
        provider_currency="INR",
        method_data_snapshot={"origin": "controlled-api-test"},
        merchant_reference=f"phase5-webhook-merchant-{attempt_id.hex}",
        idempotency_record_id=record.id,
        expires_at=order.expires_at,
    )
    session.add_all((record, attempt))
    await session.commit()
    return order, attempt


def _enable_telegram_stars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TICKET_TELEGRAM_STARS_BOT_TOKEN", "123456:phase5-api-test-token-value")
    monkeypatch.setenv("TICKET_TELEGRAM_STARS_BOT_USERNAME", "phase5_api_test_bot")
    monkeypatch.setenv("TICKET_TELEGRAM_STARS_WEBHOOK_URL", "https://example.test/webhooks/telegram")
    monkeypatch.setenv("TICKET_TELEGRAM_STARS_WEBHOOK_SECRET", "phase5-api-telegram-webhook-secret")
    monkeypatch.setenv("TICKET_TELEGRAM_STARS_INVOICE_PAYLOAD_SECRET", "phase5-api-telegram-invoice-secret")
    monkeypatch.setenv("TICKET_TELEGRAM_STARS_ALLOW_BOT_API_CALLS", "true")
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_white_label_webhook_rejects_unsigned_and_processes_signed_raw_body_once(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    namespace = "phase5_api_provider"
    secret = "phase5-api-white-label-webhook-secret"
    monkeypatch.setenv("TICKET_WHITE_LABEL_PROVIDER_NAMESPACE", namespace)
    monkeypatch.setenv("TICKET_WHITE_LABEL_WEBHOOK_SECRET", secret)
    get_settings.cache_clear()
    try:
        buyer = await _user(session, label="white-label")
        order, attempt = await _white_label_attempt(
            session, buyer=buyer, namespace=namespace, amount_paise=6_700
        )
        payload = {
            "provider_namespace": namespace,
            "external_event_id": "phase5-api-white-label-event-001",
            "event_type": "PAYMENT_SUCCEEDED",
            "merchant_reference": attempt.merchant_reference,
            "provider_payment_reference": "phase5-api-white-label-payment-001",
            "provider_amount": 6_700,
            "provider_currency": "INR",
        }
        raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        signature = hmac.new(secret.encode("utf-8"), raw_body, sha256).hexdigest()

        async def override_session():
            yield session

        app.dependency_overrides[get_session] = override_session
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                unsigned = await client.post(
                    f"/api/v1/webhooks/white-label/{namespace}",
                    content=raw_body,
                    headers={"Content-Type": "application/json"},
                )
                processed = await client.post(
                    f"/api/v1/webhooks/white-label/{namespace}",
                    content=raw_body,
                    headers={
                        "Content-Type": "application/json",
                        "X-White-Label-Signature": f"sha256={signature}",
                    },
                )
                replayed = await client.post(
                    f"/api/v1/webhooks/white-label/{namespace}",
                    content=raw_body,
                    headers={
                        "Content-Type": "application/json",
                        "X-White-Label-Signature": signature,
                    },
                )

            # Invalid or absent provider credentials are authentication
            # failures, matching the established P2P webhook boundary.
            assert unsigned.status_code == 401
            assert processed.status_code == 202, processed.text
            assert processed.json()["status"] == "PROCESSED"
            assert processed.json()["payment_attempt_id"] == str(attempt.id)
            assert processed.json()["settlement_id"] is not None
            assert processed.json()["replayed"] is False
            assert replayed.status_code == 202, replayed.text
            assert replayed.json()["replayed"] is True
        finally:
            app.dependency_overrides.clear()

        await session.refresh(order)
        await session.refresh(attempt)
        assert order.status == OrderStatus.PAID
        assert attempt.status == PaymentAttemptStatus.SUCCEEDED
        assert await session.scalar(select(func.count()).select_from(PaymentProviderEvent)) == 1
        assert await session.scalar(select(func.count()).select_from(PaymentSettlement)) == 1
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_telegram_webhook_requires_secret_and_acknowledges_authenticated_precheckout_without_settlement(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_telegram_stars(monkeypatch)
    acknowledgements: list[dict[str, object]] = []

    async def fake_answer_pre_checkout(
        self: TelegramStarsAdapter,
        *,
        pre_checkout_query_id: str,
        ok: bool,
        error_message: str | None = None,
        timeout_seconds: float = 8.0,
    ) -> None:
        del self
        acknowledgements.append(
            {
                "pre_checkout_query_id": pre_checkout_query_id,
                "ok": ok,
                "error_message": error_message,
                "timeout_seconds": timeout_seconds,
            }
        )

    monkeypatch.setattr(TelegramStarsAdapter, "answer_pre_checkout_query", fake_answer_pre_checkout)
    try:
        buyer = await _user(session, label="telegram")
        order = await _order(session, buyer=buyer, amount_paise=1_999)
        telegram_user_id = 9_876_543_210
        created = await PaymentOrchestrator(session).create_payment_attempt(
            order_id=order.id,
            buyer_user_id=buyer.id,
            method=PaymentMethod.TELEGRAM_STARS,
            telegram_user_id=telegram_user_id,
            idempotency_key="phase5-api-telegram-attempt-001",
            commit=True,
        )
        assert created.attempt.telegram_invoice_payload is not None
        update = {
            "update_id": 50_001,
            "pre_checkout_query": {
                "id": "phase5-api-precheckout-001",
                "from": {"id": telegram_user_id},
                "currency": "XTR",
                "total_amount": created.attempt.provider_amount,
                "invoice_payload": created.attempt.telegram_invoice_payload,
            },
        }
        raw_body = json.dumps(update, separators=(",", ":")).encode("utf-8")

        async def override_session():
            yield session

        app.dependency_overrides[get_session] = override_session
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                unsigned = await client.post(
                    "/api/v1/webhooks/telegram",
                    content=raw_body,
                    headers={"Content-Type": "application/json"},
                )
                processed = await client.post(
                    "/api/v1/webhooks/telegram",
                    content=raw_body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Telegram-Bot-Api-Secret-Token": "phase5-api-telegram-webhook-secret",
                    },
                )
                replayed = await client.post(
                    "/api/v1/webhooks/telegram",
                    content=raw_body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Telegram-Bot-Api-Secret-Token": "phase5-api-telegram-webhook-secret",
                    },
                )

            # Telegram uses the same configured-webhook authentication
            # boundary as the signed provider callback.
            assert unsigned.status_code == 401
            assert processed.status_code == 202, processed.text
            assert processed.json()["status"] == "PROCESSED"
            assert processed.json()["update_id"] == 50_001
            assert processed.json()["pre_checkout_query_id"] == "phase5-api-precheckout-001"
            assert processed.json()["pre_checkout_approved"] is True
            assert processed.json()["settlement_id"] is None
            assert processed.json()["replayed"] is False
            assert replayed.status_code == 202, replayed.text
            assert replayed.json()["replayed"] is True
        finally:
            app.dependency_overrides.clear()

        assert acknowledgements == [
            {
                "pre_checkout_query_id": "phase5-api-precheckout-001",
                "ok": True,
                "error_message": None,
                "timeout_seconds": 8.0,
            },
            {
                "pre_checkout_query_id": "phase5-api-precheckout-001",
                "ok": True,
                "error_message": None,
                "timeout_seconds": 8.0,
            },
        ]
        await session.refresh(order)
        await session.refresh(created.attempt)
        assert order.status == OrderStatus.AWAITING_PAYMENT
        assert created.attempt.status == PaymentAttemptStatus.AWAITING_PAYMENT
        assert await session.scalar(select(func.count()).select_from(PaymentProviderEvent)) == 1
        assert await session.scalar(select(func.count()).select_from(PaymentSettlement)) == 0
    finally:
        get_settings.cache_clear()
