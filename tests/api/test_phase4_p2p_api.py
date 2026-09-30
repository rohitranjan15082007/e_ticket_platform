"""Authorization coverage for the public Phase 4 P2P surface."""

from datetime import datetime, timedelta, timezone
import hmac
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import RoleName
from app.core.digests import canonical_payload_digest
from app.core.security import create_access_token
from app.config import get_settings
from app.database import get_session
from app.main import app
from app.models.order import DeliveryStatus, Order, OrderStatus
from app.models.user import Role, User
from app.models.withdrawal import PaymentDestination, PaymentDestinationStatus
from app.services.p2p_service import P2PService
from app.services.wallet_service import WalletService
from app.services.withdrawal_service import WithdrawalService
from app.schemas.provider_webhook import P2PProviderWebhookRequest


async def _user(session: AsyncSession, *, admin: bool = False) -> User:
    roles = [Role(name=RoleName.ADMIN.value, description="P2P API test admin")] if admin else []
    user = User(email=f"p2p-api-{uuid4()}@example.test", password_hash="test-only-hash", roles=roles)
    session.add(user)
    await session.commit()
    return user


def _authorization(user: User, *, admin: bool = False) -> dict[str, str]:
    roles = [RoleName.ADMIN.value] if admin else []
    return {"Authorization": f"Bearer {create_access_token(str(user.id), roles)}"}


async def _verified_destination_with_funded_wallet(
    session: AsyncSession, receiver: User
) -> PaymentDestination:
    wallet = (
        await WalletService(session).provision_wallet(
            user_id=receiver.id,
            actor_user_id=receiver.id,
            currency="INR",
            idempotency_key=f"p2p-api-wallet-{receiver.id}",
            commit=True,
        )
    ).wallet
    await WalletService(session).credit_available(
        wallet_id=wallet.id,
        actor_user_id=None,
        amount_paise=100_000,
        idempotency_key=f"p2p-api-credit-{receiver.id}",
        source_type="P2P_API_TEST_FUNDING",
        source_id=uuid4(),
        reason="test-only controlled host funding",
        commit=True,
    )
    destination = PaymentDestination(
        user_id=receiver.id,
        provider_namespace="upi",
        display_label="R***@upi",
        destination_data={"upi_id": "receiver@upi"},
        status=PaymentDestinationStatus.VERIFIED,
        verification_method="controlled-test-verification",
        verification_evidence_reference="p2p-api-evidence-001",
        verified_at=datetime.now(timezone.utc),
    )
    session.add(destination)
    await session.commit()
    return destination


@pytest.mark.asyncio
async def test_p2p_routes_restrict_other_users_and_admin_actions(session: AsyncSession) -> None:
    receiver = await _user(session)
    buyer = await _user(session)
    outsider = await _user(session)
    destination = await _verified_destination_with_funded_wallet(session, receiver)
    order = Order(
        buyer_user_id=buyer.id,
        status=OrderStatus.PENDING_PAYMENT,
        delivery_status=DeliveryStatus.NOT_STARTED,
        total_paise=50_000,
        currency="INR",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
    )
    session.add(order)
    await session.commit()

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            withdrawal = await client.post(
                "/api/v1/withdrawals",
                headers={**_authorization(receiver), "Idempotency-Key": "p2p-api-withdrawal-001"},
                json={"amount_paise": 50_000, "payment_destination_id": str(destination.id)},
            )
            matched = await client.post(
                f"/api/v1/orders/{order.id}/p2p/match",
                headers={**_authorization(buyer), "Idempotency-Key": "p2p-api-match-001"},
            )
            assert matched.status_code == 200, matched.text
            match_id = matched.json()["match"]["id"]

            outsider_match = await client.get(
                f"/api/v1/matches/{match_id}", headers=_authorization(outsider)
            )
            outsider_submission = await client.post(
                f"/api/v1/matches/{match_id}/payment-submissions",
                headers={**_authorization(outsider), "Idempotency-Key": "p2p-api-outsider-proof-001"},
                json={
                    "provider_namespace": "upi",
                    "claimed_reference": "OUTSIDER-UTR-001",
                    "observed_amount_paise": 50_000,
                    "observed_currency": "INR",
                    "declared_paid_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            outsider_confirmation = await client.post(
                f"/api/v1/matches/{match_id}/receiver-confirmation",
                headers={**_authorization(outsider), "Idempotency-Key": "p2p-api-outsider-confirm-001"},
                json={"decision": "RECEIVED"},
            )
            outsider_withdrawal = await client.get(
                f"/api/v1/withdrawals/{withdrawal.json()['id']}", headers=_authorization(outsider)
            )
            buyer_admin_action = await client.post(
                f"/api/v1/admin/matches/{match_id}/close-unpaid",
                headers={**_authorization(buyer), "Idempotency-Key": "p2p-api-non-admin-close-001"},
                json={"reason": "not authorized", "evidence_reference": "not-authorized"},
            )
            buyer_refund_case = await client.post(
                "/api/v1/admin/refunds",
                headers={**_authorization(buyer), "Idempotency-Key": "p2p-api-non-admin-refund-001"},
                json={
                    "match_id": match_id,
                    "amount_paise": 50_000,
                    "reason": "not authorized",
                    "liable_party": "platform",
                    "funding_source_reference": "not-authorized",
                    "destination_validation_reference": "not-authorized",
                    "executor_reference": "not-authorized",
                },
            )

        assert withdrawal.status_code == 201, withdrawal.text
        assert outsider_match.status_code == 403
        assert outsider_submission.status_code == 403
        assert outsider_confirmation.status_code == 403
        assert outsider_withdrawal.status_code == 403
        assert buyer_admin_action.status_code == 403
        assert buyer_refund_case.status_code == 403
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_admin_refund_route_creates_only_pending_evidence_case(session: AsyncSession) -> None:
    receiver = await _user(session)
    buyer = await _user(session)
    admin = await _user(session, admin=True)
    destination = await _verified_destination_with_funded_wallet(session, receiver)
    withdrawal = await WithdrawalService(session).create(
        actor_user_id=receiver.id,
        amount_paise=50_000,
        payment_destination_id=destination.id,
        idempotency_key="p2p-api-refund-withdrawal-001",
        commit=True,
    )
    order = Order(
        buyer_user_id=buyer.id,
        status=OrderStatus.PENDING_PAYMENT,
        delivery_status=DeliveryStatus.NOT_STARTED,
        total_paise=50_000,
        currency="INR",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
    )
    session.add(order)
    await session.commit()
    p2p = P2PService(session)
    matched = await p2p.queue_or_match_order(
        order_id=order.id,
        buyer_user_id=buyer.id,
        idempotency_key="p2p-api-refund-match-001",
        commit=True,
    )
    assert matched.match is not None
    submitted = await p2p.submit_payment(
        match_id=matched.match.id,
        buyer_user_id=buyer.id,
        provider_namespace="upi",
        claimed_reference="P2P-API-REFUND-UTR-001",
        observed_amount_paise=50_000,
        observed_currency="INR",
        declared_paid_at=datetime.now(timezone.utc),
        evidence_upload_id=None,
        provider_evidence_reference=None,
        evidence_metadata=None,
        idempotency_key="p2p-api-refund-submit-001",
        commit=True,
    )
    await p2p.admin_settle(
        match_id=matched.match.id,
        actor_user_id=admin.id,
        payment_submission_id=UUID(str(submitted.response_payload["submission"]["id"])),
        reason="Original receipt was independently reconciled",
        evidence_reference="p2p-api-refund-settlement-evidence-001",
        verification_source="MANUAL_RECONCILIATION",
        dispute_id=None,
        idempotency_key="p2p-api-refund-settle-001",
        commit=True,
    )

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post(
                "/api/v1/admin/refunds",
                headers={**_authorization(admin, admin=True), "Idempotency-Key": "p2p-api-refund-case-001"},
                json={
                    "match_id": str(matched.match.id),
                    "amount_paise": 50_000,
                    "reason": "Buyer payout needs controlled review",
                    "liable_party": "platform",
                    "funding_source_reference": "approved-refund-fund-001",
                    "destination_validation_reference": "verified-buyer-destination-001",
                    "executor_reference": "refund-operations-team",
                },
            )
            unsafe_payload = await client.post(
                "/api/v1/admin/refunds",
                headers={**_authorization(admin, admin=True), "Idempotency-Key": "p2p-api-refund-case-unsafe-001"},
                json={
                    "match_id": str(matched.match.id),
                    "amount_paise": 50_000,
                    "reason": "Buyer payout needs controlled review",
                    "liable_party": "platform",
                    "funding_source_reference": "approved-refund-fund-001",
                    "destination_validation_reference": "verified-buyer-destination-001",
                    "executor_reference": "refund-operations-team",
                    "payout_reference": "untrusted-payout",
                },
            )

        assert withdrawal.withdrawal.id == matched.match.withdrawal_id
        assert created.status_code == 201, created.text
        assert created.json()["status"] == "PENDING_EVIDENCE"
        assert created.json()["payout_reference"] is None
        assert unsafe_payload.status_code == 422
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_signed_provider_webhook_rejects_unsigned_callbacks_and_replays_safely(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    receiver = await _user(session)
    buyer = await _user(session)
    destination = await _verified_destination_with_funded_wallet(session, receiver)
    withdrawal = await WithdrawalService(session).create(
        actor_user_id=receiver.id,
        amount_paise=50_000,
        payment_destination_id=destination.id,
        idempotency_key="p2p-api-provider-withdrawal-001",
        commit=True,
    )
    order = Order(
        buyer_user_id=buyer.id,
        status=OrderStatus.PENDING_PAYMENT,
        delivery_status=DeliveryStatus.NOT_STARTED,
        total_paise=50_000,
        currency="INR",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
    )
    session.add(order)
    await session.commit()
    p2p = P2PService(session)
    matched = await p2p.queue_or_match_order(
        order_id=order.id,
        buyer_user_id=buyer.id,
        idempotency_key="p2p-api-provider-match-001",
        commit=True,
    )
    assert matched.match is not None
    submitted = await p2p.submit_payment(
        match_id=matched.match.id,
        buyer_user_id=buyer.id,
        provider_namespace="upi",
        claimed_reference="P2P-API-PROVIDER-UTR-001",
        observed_amount_paise=50_000,
        observed_currency="INR",
        declared_paid_at=datetime.now(timezone.utc),
        evidence_upload_id=None,
        provider_evidence_reference=None,
        evidence_metadata=None,
        idempotency_key="p2p-api-provider-submit-001",
        commit=True,
    )
    secret = "p2p-api-provider-webhook-secret-0001"
    monkeypatch.setenv("TICKET_P2P_PROVIDER_WEBHOOK_SECRET", secret)
    get_settings.cache_clear()
    payload = P2PProviderWebhookRequest(
        provider_namespace="upi",
        external_event_id="p2p-api-provider-event-001",
        event_type="PAYMENT_VERIFIED",
        match_id=matched.match.id,
        payment_submission_id=UUID(str(submitted.response_payload["submission"]["id"])),
        transaction_reference="P2P-API-PROVIDER-UTR-001",
        verified_amount_paise=50_000,
        verified_currency="INR",
        recipient_fingerprint=canonical_payload_digest(matched.match.destination_snapshot["recipient"]),
        occurred_at=datetime.now(timezone.utc),
    )
    raw_body = payload.model_dump_json().encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), raw_body, sha256).hexdigest()

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            unsigned = await client.post(
                "/api/v1/payments/provider-webhook",
                content=raw_body,
                headers={"Content-Type": "application/json"},
            )
            processed = await client.post(
                "/api/v1/payments/provider-webhook",
                content=raw_body,
                headers={"Content-Type": "application/json", "X-P2P-Signature": f"sha256={signature}"},
            )
            replayed = await client.post(
                "/api/v1/payments/provider-webhook",
                content=raw_body,
                headers={"Content-Type": "application/json", "X-P2P-Signature": signature},
            )

        assert withdrawal.withdrawal.id == matched.match.withdrawal_id
        assert unsigned.status_code == 401
        assert processed.status_code == 202, processed.text
        assert processed.json()["status"] == "PROCESSED"
        assert processed.json()["replayed"] is False
        assert replayed.status_code == 202, replayed.text
        assert replayed.json()["replayed"] is True
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
