"""Phase 5 payment-state tests with no fake provider confirmation.

Manual UPI data is evidence until an administrator accepts it.  Telegram
pre-checkout is an invoice validation step; only a distinct successful-payment
update may create the balanced settlement.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hmac
from hashlib import sha256
import json
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.permissions import RoleName
from app.exceptions import AuthenticationError
from app.models.ledger import JournalPosting, PostingDirection
from app.models.order import DeliveryStatus, Order, OrderStatus
from app.models.payment import (
    ManualPaymentDestination,
    ManualPaymentDestinationStatus,
    ManualPaymentProofStatus,
    PaymentAttempt,
    PaymentAttemptStatus,
    PaymentMethod,
    PaymentOutboxEvent,
    PaymentOutboxEventStatus,
    PaymentProviderEvent,
    PaymentProviderEventStatus,
    PaymentReconciliationRun,
    PaymentReconciliationStatus,
    PaymentSettlement,
)
from app.models.revenue_allocation import RevenueAllocation
from app.models.referral import ReferralReward, ReferralRewardStatus
from app.models.cashback import CashbackReward, CashbackRewardStatus, CashbackRewardType
from app.models.user import IdempotencyRecord, Role, User
from app.schemas.payment_methods import TelegramUpdate, WhiteLabelWebhookRequest
from app.services.ledger_service import LedgerService
from app.services.cashback_service import CashbackService
from app.services.payment_outbox_service import PaymentOutboxService
from app.services.payment_orchestrator import PaymentOrchestrator
from app.services.reconciliation_service import PaymentReconciliationService
from app.services.referral_service import ReferralService


async def _user(session: AsyncSession, *, label: str, admin: bool = False) -> User:
    roles: list[Role] = []
    if admin:
        role = await session.scalar(select(Role).where(Role.name == RoleName.ADMIN.value))
        if role is None:
            role = Role(name=RoleName.ADMIN.value, description="payment-test administrator")
        roles = [role]
    user = User(
        email=f"phase5-{label}-{uuid4()}@example.com",
        password_hash="test-only-not-used-for-authentication",
        roles=roles,
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


def _enable_manual_upi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TICKET_MANUAL_UPI_ENABLED", "true")
    get_settings.cache_clear()


def _enable_telegram_stars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TICKET_TELEGRAM_STARS_BOT_TOKEN", "123456:phase5-test-token-value")
    monkeypatch.setenv("TICKET_TELEGRAM_STARS_BOT_USERNAME", "phase5_payment_test_bot")
    monkeypatch.setenv("TICKET_TELEGRAM_STARS_WEBHOOK_URL", "https://example.test/webhooks/telegram")
    monkeypatch.setenv("TICKET_TELEGRAM_STARS_WEBHOOK_SECRET", "phase5-telegram-webhook-secret")
    monkeypatch.setenv("TICKET_TELEGRAM_STARS_INVOICE_PAYLOAD_SECRET", "phase5-telegram-invoice-secret")
    monkeypatch.setenv("TICKET_TELEGRAM_STARS_ALLOW_BOT_API_CALLS", "true")
    monkeypatch.setenv("TICKET_TELEGRAM_STARS_PER_INR", "1")
    get_settings.cache_clear()


async def _create_manual_attempt(
    session: AsyncSession, *, buyer: User, admin: User, amount_paise: int
) -> tuple[Order, PaymentAttempt]:
    service = PaymentOrchestrator(session)
    await service.create_manual_destination(
        actor_user_id=admin.id,
        display_label="Phase 5 verified merchant",
        upi_id="phase5-merchant@upi",
        qr_reference="storage://phase5/merchant-qr.png",
        instructions={"support": "Keep the UTR for review."},
        approval_evidence_reference="ops-approval-phase5-001",
        idempotency_key=f"manual-destination-{uuid4()}",
        commit=True,
    )
    order = await _order(session, buyer=buyer, amount_paise=amount_paise)
    created = await service.create_payment_attempt(
        order_id=order.id,
        buyer_user_id=buyer.id,
        method=PaymentMethod.MANUAL_UPI,
        telegram_user_id=None,
        idempotency_key=f"manual-attempt-{uuid4()}",
        commit=True,
    )
    assert created.attempt.status == PaymentAttemptStatus.AWAITING_PAYMENT
    return order, created.attempt


@pytest.mark.asyncio
async def test_manual_proof_requires_review_then_admin_settles_once(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_manual_upi(monkeypatch)
    try:
        buyer = await _user(session, label="manual-buyer")
        admin = await _user(session, label="manual-admin", admin=True)
        referrer = await _user(session, label="manual-referrer")
        order, attempt = await _create_manual_attempt(session, buyer=buyer, admin=admin, amount_paise=12_345)
        referrals = ReferralService(session)
        program = await referrals.create_program(
            actor_user_id=admin.id, name="Payment path referral", description=None,
            referrer_reward_paise=100, referred_reward_paise=50,
            minimum_order_paise=1_000, starts_at=None, ends_at=None,
            idempotency_key="manual-referral-program-001", commit=True,
        )
        await referrals.activate_program(
            program_id=program.resource.id, actor_user_id=admin.id,
            idempotency_key="manual-referral-activate-001", commit=True,
        )
        profile = await referrals.create_profile(
            user_id=referrer.id, idempotency_key="manual-referral-profile-001", commit=True
        )
        await referrals.claim(
            referred_user_id=buyer.id, referral_code=profile.resource.code,
            idempotency_key="manual-referral-claim-001", commit=True,
        )
        cashback = CashbackService(session)
        campaign, _, _ = await cashback.create_campaign(
            actor_user_id=admin.id, code="MANUALBACK", name="Payment path cashback",
            description=None, reward_type=CashbackRewardType.FIXED_PAISE,
            fixed_reward_paise=75, percentage_bps=None, max_reward_paise=None,
            minimum_order_paise=1_000, starts_at=None, ends_at=None,
            idempotency_key="manual-cashback-create-001", commit=True,
        )
        await cashback.activate_campaign(
            campaign_id=campaign.id, actor_user_id=admin.id,
            idempotency_key="manual-cashback-activate-001", commit=True,
        )
        service = PaymentOrchestrator(session)

        proof_key = "manual-proof-replay-001"
        submitted = await service.submit_manual_proof(
            attempt_id=attempt.id,
            buyer_user_id=buyer.id,
            utr="manual-utr-exact-001",
            proof_reference="storage://phase5/proofs/exact.png",
            proof_metadata={"bank": "test bank"},
            submitted_amount_paise=12_345,
            submitted_currency="INR",
            idempotency_key=proof_key,
            commit=True,
        )

        assert submitted.proof is not None
        assert submitted.proof.status == ManualPaymentProofStatus.SUBMITTED
        assert submitted.attempt.status == PaymentAttemptStatus.UNDER_REVIEW
        assert order.status == OrderStatus.PAYMENT_REVIEW
        assert await session.scalar(select(func.count()).select_from(PaymentSettlement)) == 0
        assert await session.scalar(select(func.count()).select_from(JournalPosting)) == 0

        # A retry of evidence submission must return the retained evidence,
        # rather than reject it because the first request changed the state.
        submitted_replay = await service.submit_manual_proof(
            attempt_id=attempt.id,
            buyer_user_id=buyer.id,
            utr="manual-utr-exact-001",
            proof_reference="storage://phase5/proofs/exact.png",
            proof_metadata={"bank": "test bank"},
            submitted_amount_paise=12_345,
            submitted_currency="INR",
            idempotency_key=proof_key,
            commit=True,
        )
        assert submitted_replay.replayed
        assert submitted_replay.proof is not None and submitted_replay.proof.id == submitted.proof.id

        review_key = "manual-review-approve-replay-001"
        approved = await service.review_manual_proof(
            attempt_id=attempt.id,
            actor_user_id=admin.id,
            decision="APPROVE",
            review_note="Bank receipt and amount independently verified.",
            idempotency_key=review_key,
            commit=True,
        )

        assert approved.proof is not None
        assert approved.proof.status == ManualPaymentProofStatus.APPROVED
        assert approved.attempt.status == PaymentAttemptStatus.SUCCEEDED
        assert order.status == OrderStatus.PAID
        assert order.delivery_status == DeliveryStatus.PENDING
        settlement_id = approved.response_payload["settlement_id"]
        assert settlement_id is not None
        settlement = await session.scalar(
            select(PaymentSettlement).where(PaymentSettlement.payment_attempt_id == attempt.id)
        )
        assert settlement is not None
        assert settlement.manual_payment_proof_id == submitted.proof.id
        assert settlement.verification_source == "ADMIN_MANUAL_UPI_REVIEW"
        assert await session.scalar(select(func.count()).select_from(PaymentOutboxEvent)) == 1
        referral_rewards = list(await session.scalars(select(ReferralReward)))
        cashback_rewards = list(await session.scalars(select(CashbackReward)))
        assert sorted(reward.amount_paise for reward in referral_rewards) == [50, 100]
        assert all(reward.status == ReferralRewardStatus.PENDING_REVIEW for reward in referral_rewards)
        assert len(cashback_rewards) == 1 and cashback_rewards[0].amount_paise == 75
        assert cashback_rewards[0].status == CashbackRewardStatus.PENDING_REVIEW
        postings = list(
            await session.scalars(
                select(JournalPosting).where(JournalPosting.journal_group_id == settlement.journal_group_id)
            )
        )
        assert len(postings) == 2
        assert {posting.direction for posting in postings} == {PostingDirection.DEBIT, PostingDirection.CREDIT}
        assert {posting.amount_paise for posting in postings} == {12_345}
        await LedgerService(session).assert_group_balanced(settlement.journal_group_id)

        # An approval retry is safe even though the proof is now terminal.
        approved_replay = await service.review_manual_proof(
            attempt_id=attempt.id,
            actor_user_id=admin.id,
            decision="APPROVE",
            review_note="Bank receipt and amount independently verified.",
            idempotency_key=review_key,
            commit=True,
        )
        assert approved_replay.replayed
        assert await session.scalar(select(func.count()).select_from(PaymentSettlement)) == 1
        assert await session.scalar(select(func.count()).select_from(ReferralReward)) == 2
        assert await session.scalar(select(func.count()).select_from(CashbackReward)) == 1
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_manual_destination_replacement_leaves_exactly_one_active_row(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The logical lock and partial unique index protect the active singleton."""

    _enable_manual_upi(monkeypatch)
    try:
        admin = await _user(session, label="destination-replacement-admin", admin=True)
        service = PaymentOrchestrator(session)
        first = await service.create_manual_destination(
            actor_user_id=admin.id,
            display_label="First approved merchant",
            upi_id="first-phase5-merchant@upi",
            qr_reference=None,
            instructions={"support": "first destination"},
            approval_evidence_reference="ops-approval-destination-first",
            idempotency_key="manual-destination-first-001",
            commit=True,
        )
        second = await service.create_manual_destination(
            actor_user_id=admin.id,
            display_label="Replacement approved merchant",
            upi_id="replacement-phase5-merchant@upi",
            qr_reference=None,
            instructions={"support": "replacement destination"},
            approval_evidence_reference="ops-approval-destination-second",
            idempotency_key="manual-destination-second-001",
            commit=True,
        )

        destinations = list(await session.scalars(select(ManualPaymentDestination)))
        by_id = {str(destination.id): destination for destination in destinations}
        assert len(destinations) == 2
        assert first["id"] != second["id"]
        assert by_id[first["id"]].status == ManualPaymentDestinationStatus.DISABLED
        assert by_id[second["id"]].status == ManualPaymentDestinationStatus.ACTIVE
        assert sum(
            destination.status == ManualPaymentDestinationStatus.ACTIVE for destination in destinations
        ) == 1
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_manual_approval_amount_mismatch_stays_in_review_without_settlement(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_manual_upi(monkeypatch)
    try:
        buyer = await _user(session, label="manual-mismatch-buyer")
        admin = await _user(session, label="manual-mismatch-admin", admin=True)
        order, attempt = await _create_manual_attempt(session, buyer=buyer, admin=admin, amount_paise=4_200)
        service = PaymentOrchestrator(session)
        submitted = await service.submit_manual_proof(
            attempt_id=attempt.id,
            buyer_user_id=buyer.id,
            utr="manual-utr-mismatch-001",
            proof_reference="storage://phase5/proofs/mismatch.png",
            proof_metadata=None,
            submitted_amount_paise=4_199,
            submitted_currency="INR",
            idempotency_key="manual-proof-mismatch-001",
            commit=True,
        )
        assert submitted.proof is not None

        reviewed = await service.review_manual_proof(
            attempt_id=attempt.id,
            actor_user_id=admin.id,
            decision="APPROVE",
            review_note="The submitted receipt does not show the frozen amount.",
            idempotency_key="manual-review-mismatch-001",
            commit=True,
        )

        assert reviewed.proof is not None
        assert reviewed.proof.status == ManualPaymentProofStatus.UNDER_REVIEW
        assert reviewed.attempt.status == PaymentAttemptStatus.UNDER_REVIEW
        assert order.status == OrderStatus.PAYMENT_REVIEW
        assert reviewed.response_payload["review_required"] is True
        assert reviewed.response_payload["settlement_id"] is None
        assert await session.scalar(select(func.count()).select_from(PaymentSettlement)) == 0
        assert await session.scalar(select(func.count()).select_from(JournalPosting)) == 0
    finally:
        get_settings.cache_clear()


def _pre_checkout_update(*, update_id: int, invoice_payload: str, stars: int, telegram_user_id: int) -> TelegramUpdate:
    return TelegramUpdate.model_validate(
        {
            "update_id": update_id,
            "pre_checkout_query": {
                "id": f"precheckout-{update_id}",
                "from": {"id": telegram_user_id},
                "currency": "XTR",
                "total_amount": stars,
                "invoice_payload": invoice_payload,
            },
        }
    )


def _successful_payment_update(
    *, update_id: int, invoice_payload: str, stars: int, telegram_user_id: int, charge_id: str
) -> TelegramUpdate:
    return TelegramUpdate.model_validate(
        {
            "update_id": update_id,
            "message": {
                "from": {"id": telegram_user_id},
                "successful_payment": {
                    "currency": "XTR",
                    "total_amount": stars,
                    "invoice_payload": invoice_payload,
                    "telegram_payment_charge_id": charge_id,
                },
            },
        }
    )


def _raw_update(update: TelegramUpdate) -> bytes:
    return json.dumps(update.model_dump(by_alias=True, mode="json"), separators=(",", ":")).encode("utf-8")


@pytest.mark.asyncio
async def test_telegram_pre_checkout_never_settles_and_successful_payment_is_charge_deduplicated(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_telegram_stars(monkeypatch)
    try:
        buyer = await _user(session, label="telegram-buyer")
        telegram_user_id = 9_876_543_210
        first_order = await _order(session, buyer=buyer, amount_paise=1_999)
        service = PaymentOrchestrator(session)
        first = await service.create_payment_attempt(
            order_id=first_order.id,
            buyer_user_id=buyer.id,
            method=PaymentMethod.TELEGRAM_STARS,
            telegram_user_id=telegram_user_id,
            idempotency_key="telegram-attempt-first-001",
            commit=True,
        )
        assert first.attempt.provider_currency == "XTR"
        assert first.attempt.provider_amount == 20
        assert first.attempt.telegram_invoice_payload is not None

        pre_checkout = _pre_checkout_update(
            update_id=10_001,
            invoice_payload=first.attempt.telegram_invoice_payload,
            stars=first.attempt.provider_amount,
            telegram_user_id=telegram_user_id,
        )
        pre_checkout_result = await service.ingest_telegram_update(
            payload=pre_checkout,
            raw_body=_raw_update(pre_checkout),
            webhook_secret="phase5-telegram-webhook-secret",
            commit=True,
        )
        assert pre_checkout_result.event.status == PaymentProviderEventStatus.PROCESSED
        assert pre_checkout_result.pre_checkout_approved is True
        assert pre_checkout_result.response_payload["settlement_id"] is None
        assert first.attempt.status == PaymentAttemptStatus.AWAITING_PAYMENT
        assert first_order.status == OrderStatus.AWAITING_PAYMENT
        assert await session.scalar(select(func.count()).select_from(PaymentSettlement)) == 0

        success = _successful_payment_update(
            update_id=10_002,
            invoice_payload=first.attempt.telegram_invoice_payload,
            stars=first.attempt.provider_amount,
            telegram_user_id=telegram_user_id,
            charge_id="telegram-charge-phase5-001",
        )
        successful_result = await service.ingest_telegram_update(
            payload=success,
            raw_body=_raw_update(success),
            webhook_secret="phase5-telegram-webhook-secret",
            commit=True,
        )
        assert successful_result.event.status == PaymentProviderEventStatus.PROCESSED
        assert successful_result.response_payload["settlement_id"] is not None
        assert first.attempt.status == PaymentAttemptStatus.SUCCEEDED
        assert first_order.status == OrderStatus.PAID
        assert await session.scalar(select(func.count()).select_from(PaymentSettlement)) == 1

        # A different invoice cannot claim a Telegram charge already bound to
        # the first attempt. It remains an auditable review event, not a second
        # settlement.
        second_order = await _order(session, buyer=buyer, amount_paise=2_000)
        second = await service.create_payment_attempt(
            order_id=second_order.id,
            buyer_user_id=buyer.id,
            method=PaymentMethod.TELEGRAM_STARS,
            telegram_user_id=telegram_user_id,
            idempotency_key="telegram-attempt-second-001",
            commit=True,
        )
        assert second.attempt.telegram_invoice_payload is not None
        reused_charge = _successful_payment_update(
            update_id=10_003,
            invoice_payload=second.attempt.telegram_invoice_payload,
            stars=second.attempt.provider_amount,
            telegram_user_id=telegram_user_id,
            charge_id="telegram-charge-phase5-001",
        )
        reused_result = await service.ingest_telegram_update(
            payload=reused_charge,
            raw_body=_raw_update(reused_charge),
            webhook_secret="phase5-telegram-webhook-secret",
            commit=True,
        )
        assert reused_result.event.status == PaymentProviderEventStatus.REVIEW_REQUIRED
        assert reused_result.event.processing_error == "TELEGRAM_CHARGE_REUSED"
        assert second.attempt.status == PaymentAttemptStatus.UNDER_REVIEW
        assert second_order.status == OrderStatus.PAYMENT_REVIEW
        assert await session.scalar(select(func.count()).select_from(PaymentSettlement)) == 1
        assert await session.scalar(select(func.count()).select_from(PaymentProviderEvent)) == 3
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_reconciliation_snapshot_never_settles_manual_evidence_and_replays(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_manual_upi(monkeypatch)
    try:
        buyer = await _user(session, label="reconciliation-buyer")
        admin = await _user(session, label="reconciliation-admin", admin=True)
        _, attempt = await _create_manual_attempt(session, buyer=buyer, admin=admin, amount_paise=7_500)
        await PaymentOrchestrator(session).submit_manual_proof(
            attempt_id=attempt.id,
            buyer_user_id=buyer.id,
            utr="manual-utr-reconciliation-001",
            proof_reference="storage://phase5/proofs/reconciliation.png",
            proof_metadata=None,
            submitted_amount_paise=7_500,
            submitted_currency="INR",
            idempotency_key="manual-proof-reconciliation-001",
            commit=True,
        )

        reconciliation = PaymentReconciliationService(session)
        first = await reconciliation.reconcile_scope(
            method=PaymentMethod.MANUAL_UPI,
            provider_namespace="manual_upi",
            run_key="phase5-reconciliation-manual-001",
            commit=True,
        )
        assert first.replayed is False
        assert first.run.status == PaymentReconciliationStatus.COMPLETED
        assert first.run.result_summary is not None
        assert first.run.result_summary["mode"] == "REVIEW_QUEUE_ONLY"
        assert first.run.result_summary["automatic_settlements"] == 0
        assert first.run.result_summary["pending_manual_proofs"] == 1
        assert await session.scalar(select(func.count()).select_from(PaymentSettlement)) == 0

        replay = await reconciliation.reconcile_scope(
            method=PaymentMethod.MANUAL_UPI,
            provider_namespace="manual_upi",
            run_key="phase5-reconciliation-manual-001",
            commit=True,
        )
        assert replay.replayed is True
        assert replay.run.id == first.run.id
        assert await session.scalar(select(func.count()).select_from(PaymentReconciliationRun)) == 1
        assert await session.scalar(select(func.count()).select_from(PaymentSettlement)) == 0
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_expired_payment_attempt_moves_to_review_once_without_release(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_manual_upi(monkeypatch)
    try:
        buyer = await _user(session, label="expiry-buyer")
        admin = await _user(session, label="expiry-admin", admin=True)
        order, attempt = await _create_manual_attempt(session, buyer=buyer, admin=admin, amount_paise=3_400)
        attempt.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await session.commit()

        service = PaymentOrchestrator(session)
        changed = await service.expire_payment_attempt(
            attempt_id=attempt.id,
            idempotency_key="phase5-payment-expiry-001",
            commit=True,
        )
        assert changed is True
        assert attempt.status == PaymentAttemptStatus.EXPIRED
        assert order.status == OrderStatus.PAYMENT_REVIEW
        assert await session.scalar(select(func.count()).select_from(PaymentSettlement)) == 0

        # The same buyer can retain late evidence after the worker wins, but
        # it remains a human-review path and never settles automatically.
        late_proof = await service.submit_manual_proof(
            attempt_id=attempt.id,
            buyer_user_id=buyer.id,
            utr="manual-utr-after-expiry-worker-001",
            proof_reference="storage://phase5/proofs/after-expiry-worker.png",
            proof_metadata=None,
            submitted_amount_paise=3_400,
            submitted_currency="INR",
            idempotency_key="manual-proof-after-expiry-worker-001",
            commit=True,
        )
        assert late_proof.proof is not None
        assert late_proof.attempt.status == PaymentAttemptStatus.UNDER_REVIEW
        assert late_proof.attempt.failure_code == "LATE_MANUAL_PROOF"
        assert order.status == OrderStatus.PAYMENT_REVIEW
        assert await session.scalar(select(func.count()).select_from(PaymentSettlement)) == 0

        replay = await service.expire_payment_attempt(
            attempt_id=attempt.id,
            idempotency_key="phase5-payment-expiry-001",
            commit=True,
        )
        assert replay is False
        assert await session.scalar(
            select(func.count()).select_from(IdempotencyRecord).where(
                IdempotencyRecord.actor_scope == "system:payment-attempt-expiry",
                IdempotencyRecord.idempotency_key == "phase5-payment-expiry-001",
            )
        ) == 1
        assert await session.scalar(select(func.count()).select_from(PaymentSettlement)) == 0
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_late_manual_proof_before_expiry_worker_is_review_only(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wall-clock expiry and worker ordering produce the same review outcome."""

    _enable_manual_upi(monkeypatch)
    try:
        buyer = await _user(session, label="late-proof-buyer")
        admin = await _user(session, label="late-proof-admin", admin=True)
        order, attempt = await _create_manual_attempt(session, buyer=buyer, admin=admin, amount_paise=2_750)
        attempt.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await session.commit()

        proof = await PaymentOrchestrator(session).submit_manual_proof(
            attempt_id=attempt.id,
            buyer_user_id=buyer.id,
            utr="manual-utr-before-expiry-worker-001",
            proof_reference="storage://phase5/proofs/before-expiry-worker.png",
            proof_metadata={"submitted_after_expiry": "true"},
            submitted_amount_paise=2_750,
            submitted_currency="INR",
            idempotency_key="manual-proof-before-expiry-worker-001",
            commit=True,
        )

        assert proof.proof is not None
        assert proof.attempt.status == PaymentAttemptStatus.UNDER_REVIEW
        assert proof.attempt.failure_code == "LATE_MANUAL_PROOF"
        assert order.status == OrderStatus.PAYMENT_REVIEW
        assert await session.scalar(select(func.count()).select_from(PaymentSettlement)) == 0
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_payment_outbox_failure_preserves_one_paid_settlement_for_retry(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Allocation defects may fail delivery, but can never reverse or duplicate payment."""

    _enable_manual_upi(monkeypatch)
    try:
        buyer = await _user(session, label="outbox-buyer")
        admin = await _user(session, label="outbox-admin", admin=True)
        order, attempt = await _create_manual_attempt(session, buyer=buyer, admin=admin, amount_paise=8_800)
        orchestrator = PaymentOrchestrator(session)
        submitted = await orchestrator.submit_manual_proof(
            attempt_id=attempt.id,
            buyer_user_id=buyer.id,
            utr="manual-utr-outbox-001",
            proof_reference="storage://phase5/proofs/outbox.png",
            proof_metadata=None,
            submitted_amount_paise=8_800,
            submitted_currency="INR",
            idempotency_key="manual-proof-outbox-001",
            commit=True,
        )
        assert submitted.proof is not None
        await orchestrator.review_manual_proof(
            attempt_id=attempt.id,
            actor_user_id=admin.id,
            decision="APPROVE",
            review_note="Independent bank evidence confirmed the exact amount.",
            idempotency_key="manual-review-outbox-001",
            commit=True,
        )
        outbox = await session.scalar(
            select(PaymentOutboxEvent).where(PaymentOutboxEvent.payment_attempt_id == attempt.id)
        )
        assert outbox is not None

        # This minimal direct order has no order item/reservation, so the real
        # allocator rejects it. The dispatcher must retain the paid order and
        # schedule delivery-only retry work instead of changing settlement.
        delivered = await PaymentOutboxService(session).dispatch_pending(limit=10)
        await session.refresh(order)
        await session.refresh(outbox)
        assert delivered == 0
        assert order.status == OrderStatus.PAID
        assert order.delivery_status == DeliveryStatus.FAILED
        assert outbox.status == PaymentOutboxEventStatus.FAILED
        assert outbox.attempts == 1
        assert outbox.available_at is not None
        assert await session.scalar(select(func.count()).select_from(PaymentSettlement)) == 1
        assert await session.scalar(select(func.count()).select_from(JournalPosting)) == 8
        assert await session.scalar(select(func.count()).select_from(RevenueAllocation)) == 1
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_signed_white_label_event_settles_exact_frozen_attempt_once(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The generic boundary only accepts signed exact provider facts once."""

    namespace = "phase5_test_provider"
    secret = "phase5-white-label-webhook-secret-001"
    monkeypatch.setenv("TICKET_WHITE_LABEL_PROVIDER_NAMESPACE", namespace)
    monkeypatch.setenv("TICKET_WHITE_LABEL_WEBHOOK_SECRET", secret)
    get_settings.cache_clear()
    try:
        buyer = await _user(session, label="white-label-buyer")
        order = await _order(session, buyer=buyer, amount_paise=6_700)
        order.status = OrderStatus.AWAITING_PAYMENT
        attempt_id = uuid4()
        record = IdempotencyRecord(
            id=uuid4(),
            actor_scope=f"test:white-label:create:{buyer.id}",
            idempotency_key="phase5-white-label-attempt-001",
            request_fingerprint="a" * 64,
            resource_type="payment_attempt",
            resource_id=attempt_id,
            status_code=201,
        )
        session.add(record)
        await session.flush()
        attempt = PaymentAttempt(
            id=attempt_id,
            order_id=order.id,
            buyer_user_id=buyer.id,
            method=PaymentMethod.WHITE_LABEL,
            provider_namespace=namespace,
            status=PaymentAttemptStatus.AWAITING_PAYMENT,
            order_amount_paise=6_700,
            order_currency="INR",
            provider_amount=6_700,
            provider_currency="INR",
            method_data_snapshot={"created_by": "controlled-test-provider-adapter"},
            merchant_reference="phase5-white-label-merchant-001",
            idempotency_record_id=record.id,
            expires_at=order.expires_at,
        )
        session.add(attempt)
        await session.commit()

        payload = WhiteLabelWebhookRequest(
            provider_namespace=namespace,
            external_event_id="phase5-white-label-event-001",
            event_type="PAYMENT_SUCCEEDED",
            merchant_reference=attempt.merchant_reference,
            provider_payment_reference="phase5-white-label-payment-001",
            provider_amount=6_700,
            provider_currency="INR",
            occurred_at=datetime.now(timezone.utc),
        )
        raw_body = payload.model_dump_json().encode("utf-8")
        signature = hmac.new(secret.encode("utf-8"), raw_body, sha256).hexdigest()
        service = PaymentOrchestrator(session)

        missing_reference_payload = WhiteLabelWebhookRequest(
            provider_namespace=namespace,
            external_event_id="phase5-white-label-event-missing-reference-001",
            event_type="PAYMENT_SUCCEEDED",
            merchant_reference=attempt.merchant_reference,
            provider_amount=6_700,
            provider_currency="INR",
            occurred_at=datetime.now(timezone.utc),
        )
        missing_reference_raw_body = missing_reference_payload.model_dump_json().encode("utf-8")
        missing_reference_signature = hmac.new(
            secret.encode("utf-8"), missing_reference_raw_body, sha256
        ).hexdigest()
        review_required = await service.ingest_white_label_event(
            payload=missing_reference_payload,
            raw_body=missing_reference_raw_body,
            signature=missing_reference_signature,
            request_headers={"Content-Type": "application/json"},
            commit=True,
        )
        assert review_required.event.status == PaymentProviderEventStatus.REVIEW_REQUIRED
        assert review_required.event.processing_error == "MISSING_PROVIDER_PAYMENT_REFERENCE"
        assert review_required.response_payload["settlement_id"] is None
        assert attempt.status == PaymentAttemptStatus.UNDER_REVIEW
        assert order.status == OrderStatus.PAYMENT_REVIEW
        assert await session.scalar(select(func.count()).select_from(PaymentSettlement)) == 0

        with pytest.raises(AuthenticationError):
            await service.ingest_white_label_event(
                payload=payload,
                raw_body=raw_body,
                signature="not-a-valid-signature",
                request_headers={"Content-Type": "application/json"},
                commit=True,
            )

        processed = await service.ingest_white_label_event(
            payload=payload,
            raw_body=raw_body,
            signature=f"sha256={signature}",
            request_headers={"Content-Type": "application/json", "Authorization": "never-stored"},
            commit=True,
        )
        assert processed.event.status == PaymentProviderEventStatus.PROCESSED
        assert processed.response_payload["settlement_id"] is not None
        assert attempt.status == PaymentAttemptStatus.SUCCEEDED
        assert order.status == OrderStatus.PAID
        assert await session.scalar(select(func.count()).select_from(PaymentSettlement)) == 1

        replay = await service.ingest_white_label_event(
            payload=payload,
            raw_body=raw_body,
            signature=signature,
            request_headers={"Content-Type": "application/json"},
            commit=True,
        )
        assert replay.replayed is True
        assert await session.scalar(select(func.count()).select_from(PaymentSettlement)) == 1
        assert processed.event.request_headers == {"content-type": "application/json"}
    finally:
        get_settings.cache_clear()
