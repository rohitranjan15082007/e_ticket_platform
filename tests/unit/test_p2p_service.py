"""Phase 4 P2P acceptance coverage without any fake provider approval."""

from datetime import datetime, timedelta, timezone
import hmac
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import RoleName
from app.core.digests import canonical_payload_digest
from app.core.security import hash_password
from app.config import get_settings
from app.exceptions import ConflictError, ValidationError
from app.models.ledger import JournalPosting, PostingDirection
from app.models.audit_log import AuditLog
from app.models.order import DeliveryStatus, Order, OrderStatus, TicketProductType
from app.models.p2p_match import (
    OutboxEventStatus,
    AdminResolutionDecision,
    P2PAdminResolution,
    P2PDispute,
    P2PDisputeStatus,
    P2PMatch,
    P2PMatchStatus,
    P2PNotification,
    P2POutboxEvent,
    P2PPaymentSubmission,
    P2PProviderEvent,
    P2PProviderEventStatus,
    P2PRefund,
    P2PRefundStatus,
    P2PSettlement,
    P2PVerifiedPaymentReference,
    PaymentSubmissionVerificationStatus,
    ReceiverConfirmationDecision,
)
from app.models.user import IdempotencyRecord, Role, User
from app.models.ticket import Ticket
from app.models.wallet import Wallet, WalletHold, WalletHoldStatus
from app.models.withdrawal import PaymentDestination, PaymentDestinationStatus, WithdrawalStatus
from app.services.ledger_service import LedgerService
from app.services.p2p_service import P2PService
from app.services.p2p_outbox_service import P2POutboxService
from app.services.p2p_risk_service import P2PRiskService
from app.services.provider_event_service import ProviderEventService
from app.services.dispute_service import DisputeService
from app.services.refund_service import RefundService
from app.services.order_service import OrderSelection, OrderService
from app.services.ticket_series_service import PrizeDraft, TicketSeriesService
from app.services.wallet_service import WalletService
from app.services.withdrawal_service import WithdrawalService
from app.services.winner_service import WinnerService
from app.schemas.provider_webhook import P2PProviderWebhookRequest


async def _user(session: AsyncSession, email: str, *, admin: bool = False) -> User:
    roles: list[Role] = []
    if admin:
        role = await session.scalar(select(Role).where(Role.name == RoleName.ADMIN.value))
        if role is None:
            role = Role(name=RoleName.ADMIN.value, description="test admin")
        roles = [role]
    user = User(email=email, password_hash=hash_password("correct-horse-battery-staple"), roles=roles)
    session.add(user)
    await session.commit()
    return user


async def _wallet_with_credit(session: AsyncSession, user: User, amount_paise: int) -> Wallet:
    wallets = WalletService(session)
    provisioned = await wallets.provision_wallet(
        user_id=user.id,
        actor_user_id=user.id,
        currency="INR",
        idempotency_key=f"p2p-provision-{user.id}",
        commit=True,
    )
    await wallets.credit_available(
        wallet_id=provisioned.wallet.id,
        actor_user_id=None,
        amount_paise=amount_paise,
        idempotency_key=f"p2p-credit-{user.id}",
        source_type="P2P_TEST_HOST_CREDIT",
        source_id=uuid4(),
        reason="test credit source",
        commit=True,
    )
    wallet = await session.get(Wallet, provisioned.wallet.id)
    assert wallet is not None
    return wallet


async def _verified_destination(session: AsyncSession, user: User) -> PaymentDestination:
    destination = PaymentDestination(
        user_id=user.id,
        provider_namespace="upi",
        display_label="R***@upi",
        destination_data={"upi_id": "receiver@upi"},
        status=PaymentDestinationStatus.VERIFIED,
        verification_method="controlled-test-verification",
        verification_evidence_reference="test-evidence-001",
        verified_at=datetime.now(timezone.utc),
    )
    session.add(destination)
    await session.commit()
    return destination


async def _order(session: AsyncSession, buyer: User, amount_paise: int) -> Order:
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


async def _matched_flow(
    session: AsyncSession, *, amount_paise: int = 50_000
) -> tuple[User, User, User, Wallet, Order, P2PMatch]:
    receiver = await _user(session, f"receiver-{uuid4()}@example.com")
    buyer = await _user(session, f"buyer-{uuid4()}@example.com")
    admin = await _user(session, f"admin-{uuid4()}@example.com", admin=True)
    wallet = await _wallet_with_credit(session, receiver, 100_000)
    destination = await _verified_destination(session, receiver)
    withdrawal = await WithdrawalService(session).create(
        actor_user_id=receiver.id,
        amount_paise=amount_paise,
        payment_destination_id=destination.id,
        idempotency_key=f"withdrawal-create-{uuid4()}",
        commit=True,
    )
    order = await _order(session, buyer, amount_paise)
    matched = await P2PService(session).queue_or_match_order(
        order_id=order.id,
        buyer_user_id=buyer.id,
        idempotency_key=f"p2p-match-{uuid4()}",
        commit=True,
    )
    assert matched.match is not None
    assert withdrawal.withdrawal.status == WithdrawalStatus.MATCHED
    return receiver, buyer, admin, wallet, order, matched.match


async def _open_series_for_p2p(session: AsyncSession, admin: User, *, price_paise: int):
    now = datetime.now(timezone.utc)
    series_service = TicketSeriesService(session)
    created = await series_service.create(
        actor_user_id=admin.id,
        name=f"P2P delivery series {uuid4()}",
        description="P2P delivery integration coverage",
        price_paise=price_paise,
        ticket_limit=2,
        sales_start_at=now - timedelta(minutes=1),
        sales_end_at=now + timedelta(hours=1),
        draw_at=now + timedelta(days=1),
        prizes=[PrizeDraft(rank=1, title="P2P prize", prize_paise=price_paise)],
        idempotency_key=f"p2p-series-create-{uuid4()}",
        commit=True,
    )
    await series_service.publish(
        series_id=created.series.id,
        actor_user_id=admin.id,
        idempotency_key=f"p2p-series-publish-{uuid4()}",
        commit=True,
    )
    await WinnerService(session).commit_seed(
        series_id=created.series.id,
        actor_user_id=admin.id,
        seed_commitment=sha256(f"p2p-series-draw-seed-{created.series.id}".encode("utf-8")).hexdigest(),
        idempotency_key=f"p2p-series-draw-commit-{uuid4()}",
        commit=True,
    )
    opened = await series_service.open(
        series_id=created.series.id,
        actor_user_id=admin.id,
        idempotency_key=f"p2p-series-open-{uuid4()}",
        commit=True,
    )
    return opened.series


@pytest.mark.asyncio
async def test_default_50_percent_hold_is_snapshotted_and_second_request_is_blocked(
    session: AsyncSession,
) -> None:
    receiver = await _user(session, "withdrawal-default@example.com")
    wallet = await _wallet_with_credit(session, receiver, 100_000)
    destination = await _verified_destination(session, receiver)
    service = WithdrawalService(session)

    created = await service.create(
        actor_user_id=receiver.id,
        amount_paise=50_000,
        payment_destination_id=destination.id,
        idempotency_key="p2p-withdrawal-default-001",
        commit=True,
    )
    refreshed = await session.get(Wallet, wallet.id)
    hold = await session.get(WalletHold, created.withdrawal.wallet_hold_id)

    assert refreshed is not None
    assert (refreshed.available_paise, refreshed.locked_paise) == (50_000, 50_000)
    assert hold is not None and hold.status == WalletHoldStatus.ACTIVE
    assert created.withdrawal.max_amount_snapshot_paise == 50_000
    assert created.withdrawal.rule_snapshot["percentage_bps"] == 5000
    with pytest.raises(ConflictError, match="ACTIVE_WITHDRAWAL_EXISTS"):
        await service.create(
            actor_user_id=receiver.id,
            amount_paise=5_000,
            payment_destination_id=destination.id,
            idempotency_key="p2p-withdrawal-default-002",
            commit=True,
        )


@pytest.mark.asyncio
async def test_strict_ceiling_rejects_one_paise_over_and_500_balance_allows_250(
    session: AsyncSession,
) -> None:
    receiver = await _user(session, "withdrawal-ceiling@example.com")
    await _wallet_with_credit(session, receiver, 100_000)
    destination = await _verified_destination(session, receiver)
    service = WithdrawalService(session)

    with pytest.raises(ValidationError, match="INSUFFICIENT_ELIGIBLE_BALANCE"):
        await service.create(
            actor_user_id=receiver.id,
            amount_paise=50_001,
            payment_destination_id=destination.id,
            idempotency_key="p2p-withdrawal-over-ceiling-001",
            commit=True,
        )
    at_ceiling = await service.create(
        actor_user_id=receiver.id,
        amount_paise=50_000,
        payment_destination_id=destination.id,
        idempotency_key="p2p-withdrawal-at-ceiling-001",
        commit=True,
    )
    assert at_ceiling.withdrawal.max_amount_snapshot_paise == 50_000

    smaller_receiver = await _user(session, "withdrawal-five-hundred@example.com")
    await _wallet_with_credit(session, smaller_receiver, 50_000)
    smaller_destination = await _verified_destination(session, smaller_receiver)
    allowed = await WithdrawalService(session).create(
        actor_user_id=smaller_receiver.id,
        amount_paise=25_000,
        payment_destination_id=smaller_destination.id,
        idempotency_key="p2p-withdrawal-five-hundred-001",
        commit=True,
    )
    assert allowed.withdrawal.max_amount_snapshot_paise == 25_000


@pytest.mark.asyncio
async def test_rule_change_uses_new_formula_without_mutating_existing_snapshot(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_receiver = await _user(session, "withdrawal-old-rule@example.com")
    await _wallet_with_credit(session, original_receiver, 100_000)
    original_destination = await _verified_destination(session, original_receiver)
    original = await WithdrawalService(session).create(
        actor_user_id=original_receiver.id,
        amount_paise=50_000,
        payment_destination_id=original_destination.id,
        idempotency_key="p2p-withdrawal-old-rule-001",
        commit=True,
    )

    monkeypatch.setenv("TICKET_P2P_WITHDRAWAL_PERCENTAGE_BPS", "2500")
    get_settings.cache_clear()
    try:
        new_receiver = await _user(session, "withdrawal-new-rule@example.com")
        await _wallet_with_credit(session, new_receiver, 100_000)
        new_destination = await _verified_destination(session, new_receiver)
        new = await WithdrawalService(session).create(
            actor_user_id=new_receiver.id,
            amount_paise=25_000,
            payment_destination_id=new_destination.id,
            idempotency_key="p2p-withdrawal-new-rule-001",
            commit=True,
        )
        assert new.withdrawal.max_amount_snapshot_paise == 25_000
        assert new.withdrawal.rule_snapshot["percentage_bps"] == 2500
        assert original.withdrawal.max_amount_snapshot_paise == 50_000
        assert original.withdrawal.rule_snapshot["percentage_bps"] == 5000
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_exact_matching_uses_stored_request_not_a_recomputed_current_ceiling(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    receiver = await _user(session, "snapshot-match-receiver@example.com")
    await _wallet_with_credit(session, receiver, 200_000)
    destination = await _verified_destination(session, receiver)
    withdrawal = await WithdrawalService(session).create(
        actor_user_id=receiver.id,
        amount_paise=100_000,
        payment_destination_id=destination.id,
        idempotency_key="p2p-snapshot-match-withdrawal-001",
        commit=True,
    )
    assert withdrawal.withdrawal.max_amount_snapshot_paise == 100_000

    monkeypatch.setenv("TICKET_P2P_WITHDRAWAL_PERCENTAGE_BPS", "2500")
    get_settings.cache_clear()
    try:
        smaller_buyer = await _user(session, "snapshot-match-small-buyer@example.com")
        smaller_order = await _order(session, smaller_buyer, 50_000)
        no_partial = await P2PService(session).queue_or_match_order(
            order_id=smaller_order.id,
            buyer_user_id=smaller_buyer.id,
            idempotency_key="p2p-snapshot-match-small-001",
            commit=True,
        )
        exact_buyer = await _user(session, "snapshot-match-exact-buyer@example.com")
        exact_order = await _order(session, exact_buyer, 100_000)
        exact = await P2PService(session).queue_or_match_order(
            order_id=exact_order.id,
            buyer_user_id=exact_buyer.id,
            idempotency_key="p2p-snapshot-match-exact-001",
            commit=True,
        )

        assert no_partial.match is None
        assert no_partial.response_payload["order"]["status"] == OrderStatus.WAITING_FOR_MATCH.value
        assert exact.match is not None
        assert exact.match.withdrawal_id == withdrawal.withdrawal.id
        assert exact.match.amount_paise == 100_000
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_exact_match_proof_and_receiver_confirmation_do_not_auto_settle(
    session: AsyncSession,
) -> None:
    receiver, buyer, _, wallet, order, match = await _matched_flow(session)
    p2p = P2PService(session)
    submitted = await p2p.submit_payment(
        match_id=match.id,
        buyer_user_id=buyer.id,
        provider_namespace="upi",
        claimed_reference="UTR-UNVERIFIED-001",
        observed_amount_paise=50_000,
        observed_currency="INR",
        declared_paid_at=datetime.now(timezone.utc),
        evidence_upload_id=uuid4(),
        provider_evidence_reference=None,
        evidence_metadata={"screen": "uploaded"},
        idempotency_key="p2p-submit-unverified-001",
        commit=True,
    )
    confirmation = await p2p.confirm_receipt(
        match_id=match.id,
        receiver_user_id=receiver.id,
        decision=ReceiverConfirmationDecision.RECEIVED,
        reason="Receiver confirms the claim but provider verification is pending",
        idempotency_key="p2p-confirm-unverified-001",
        commit=True,
    )
    refreshed_wallet = await session.get(Wallet, wallet.id)
    refreshed_order = await session.get(Order, order.id)
    settlements = await session.scalar(select(func.count()).select_from(P2PSettlement))

    assert submitted.match is not None
    assert confirmation.match is not None
    assert confirmation.match.status == P2PMatchStatus.UNDER_VERIFICATION
    assert refreshed_wallet is not None and (refreshed_wallet.available_paise, refreshed_wallet.locked_paise) == (50_000, 50_000)
    assert refreshed_order is not None and refreshed_order.status == OrderStatus.AWAITING_PAYMENT
    assert settlements == 0


@pytest.mark.asyncio
async def test_evidenced_admin_override_settles_once_and_posts_pending_fulfillment(
    session: AsyncSession,
) -> None:
    receiver, buyer, admin, wallet, order, match = await _matched_flow(session)
    p2p = P2PService(session)
    submitted = await p2p.submit_payment(
        match_id=match.id,
        buyer_user_id=buyer.id,
        provider_namespace="upi",
        claimed_reference="UTR-ADMIN-001",
        observed_amount_paise=50_000,
        observed_currency="INR",
        declared_paid_at=datetime.now(timezone.utc),
        evidence_upload_id=uuid4(),
        provider_evidence_reference=None,
        evidence_metadata=None,
        idempotency_key="p2p-submit-admin-001",
        commit=True,
    )
    submission_id = submitted.response_payload["submission"]["id"]
    first = await p2p.admin_settle(
        match_id=match.id,
        actor_user_id=admin.id,
        payment_submission_id=UUID(str(submission_id)),
        reason="Bank-side reconciliation evidence matched the receiver destination",
        evidence_reference="reconciliation-case-001",
        verification_source="MANUAL_RECONCILIATION",
        dispute_id=None,
        idempotency_key="p2p-admin-settle-001",
        commit=True,
    )
    replay = await p2p.admin_settle(
        match_id=match.id,
        actor_user_id=admin.id,
        payment_submission_id=UUID(str(submission_id)),
        reason="Bank-side reconciliation evidence matched the receiver destination",
        evidence_reference="reconciliation-case-001",
        verification_source="MANUAL_RECONCILIATION",
        dispute_id=None,
        idempotency_key="p2p-admin-settle-001",
        commit=True,
    )
    refreshed_wallet = await session.get(Wallet, wallet.id)
    refreshed_order = await session.get(Order, order.id)
    hold = await session.scalar(select(WalletHold).where(WalletHold.wallet_id == wallet.id))
    settlement = await session.scalar(select(P2PSettlement))

    assert first.match is not None and first.match.status == P2PMatchStatus.SETTLED
    assert replay.replayed
    assert refreshed_wallet is not None and (refreshed_wallet.available_paise, refreshed_wallet.locked_paise) == (50_000, 0)
    assert refreshed_order is not None
    assert (refreshed_order.status, refreshed_order.delivery_status) == (OrderStatus.PAID, DeliveryStatus.PENDING)
    assert hold is not None and hold.status == WalletHoldStatus.SETTLED
    assert settlement is not None
    postings = list(
        await session.scalars(select(JournalPosting).where(JournalPosting.journal_group_id == settlement.journal_group_id))
    )
    assert len(postings) == 2
    assert {posting.direction for posting in postings} == {PostingDirection.DEBIT, PostingDirection.CREDIT}
    await LedgerService(session).assert_group_balanced(settlement.journal_group_id)


@pytest.mark.asyncio
async def test_unresolved_dispute_blocks_new_request_then_settlement_uses_current_balance(
    session: AsyncSession,
) -> None:
    receiver, buyer, admin, wallet, _, match = await _matched_flow(session)
    destination = await session.scalar(
        select(PaymentDestination).where(PaymentDestination.user_id == receiver.id)
    )
    assert destination is not None
    p2p = P2PService(session)
    submitted = await p2p.submit_payment(
        match_id=match.id,
        buyer_user_id=buyer.id,
        provider_namespace="upi",
        claimed_reference="UTR-NEXT-CYCLE-001",
        observed_amount_paise=50_000,
        observed_currency="INR",
        declared_paid_at=datetime.now(timezone.utc),
        evidence_upload_id=None,
        provider_evidence_reference=None,
        evidence_metadata=None,
        idempotency_key="p2p-next-cycle-submit-001",
        commit=True,
    )
    await p2p.confirm_receipt(
        match_id=match.id,
        receiver_user_id=receiver.id,
        decision=ReceiverConfirmationDecision.NOT_RECEIVED,
        reason="Disputed before settlement",
        idempotency_key="p2p-next-cycle-denial-001",
        commit=True,
    )
    with pytest.raises(ConflictError, match="ACTIVE_WITHDRAWAL_EXISTS"):
        await WithdrawalService(session).create(
            actor_user_id=receiver.id,
            amount_paise=5_000,
            payment_destination_id=destination.id,
            idempotency_key="p2p-next-cycle-blocked-001",
            commit=True,
        )

    settled = await p2p.admin_settle(
        match_id=match.id,
        actor_user_id=admin.id,
        payment_submission_id=UUID(str(submitted.response_payload["submission"]["id"])),
        reason="Bank evidence resolves the receiver dispute",
        evidence_reference="next-cycle-reconciliation-001",
        verification_source="MANUAL_RECONCILIATION",
        dispute_id=(await session.scalar(select(P2PDispute.id).where(P2PDispute.match_id == match.id))),
        idempotency_key="p2p-next-cycle-settle-001",
        commit=True,
    )
    next_request = await WithdrawalService(session).create(
        actor_user_id=receiver.id,
        amount_paise=25_000,
        payment_destination_id=destination.id,
        idempotency_key="p2p-next-cycle-new-request-001",
        commit=True,
    )
    refreshed_wallet = await session.get(Wallet, wallet.id)
    resolutions = await session.scalar(select(func.count()).select_from(P2PAdminResolution))

    assert settled.match is not None and settled.match.status == P2PMatchStatus.SETTLED
    assert next_request.withdrawal.max_amount_snapshot_paise == 25_000
    assert refreshed_wallet is not None and (refreshed_wallet.available_paise, refreshed_wallet.locked_paise) == (25_000, 25_000)
    assert resolutions == 1


@pytest.mark.asyncio
async def test_expiry_retains_hold_and_receiver_denial_creates_dispute(session: AsyncSession) -> None:
    receiver, buyer, _, wallet, _, match = await _matched_flow(session)
    p2p = P2PService(session)
    match.payment_deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await session.commit()
    expired = await p2p.expire_match(
        match_id=match.id, idempotency_key="p2p-expire-001", commit=True
    )
    wallet_after_expiry = await session.get(Wallet, wallet.id)
    assert expired.match is not None and expired.match.status == P2PMatchStatus.EXPIRED_AWAITING_RECONCILIATION
    assert wallet_after_expiry is not None and wallet_after_expiry.locked_paise == 50_000
    destination = await session.scalar(
        select(PaymentDestination).where(PaymentDestination.user_id == receiver.id)
    )
    assert destination is not None
    with pytest.raises(ConflictError, match="ACTIVE_WITHDRAWAL_EXISTS"):
        await WithdrawalService(session).create(
            actor_user_id=receiver.id,
            amount_paise=5_000,
            payment_destination_id=destination.id,
            idempotency_key="p2p-expired-withdrawal-blocked-001",
            commit=True,
        )
    waiting_buyer = await _user(session, f"expired-rematch-buyer-{uuid4()}@example.com")
    waiting_order = await _order(session, waiting_buyer, 50_000)
    no_automatic_rematch = await p2p.queue_or_match_order(
        order_id=waiting_order.id,
        buyer_user_id=waiting_buyer.id,
        idempotency_key="p2p-expired-no-rematch-001",
        commit=True,
    )
    assert no_automatic_rematch.match is None

    # A new match demonstrates the receiver-denial dispute path separately.
    receiver2, buyer2, _, _, _, match2 = await _matched_flow(session)
    await p2p.submit_payment(
        match_id=match2.id,
        buyer_user_id=buyer2.id,
        provider_namespace="upi",
        claimed_reference="UTR-DENIAL-001",
        observed_amount_paise=50_000,
        observed_currency="INR",
        declared_paid_at=datetime.now(timezone.utc),
        evidence_upload_id=None,
        provider_evidence_reference=None,
        evidence_metadata=None,
        idempotency_key="p2p-submit-denial-001",
        commit=True,
    )
    denied = await p2p.confirm_receipt(
        match_id=match2.id,
        receiver_user_id=receiver2.id,
        decision=ReceiverConfirmationDecision.NOT_RECEIVED,
        reason="Receiver did not find this payment",
        idempotency_key="p2p-denial-001",
        commit=True,
    )
    dispute = await session.scalar(select(P2PDispute).where(P2PDispute.match_id == match2.id))
    assert denied.match is not None and denied.match.status == P2PMatchStatus.DISPUTED
    assert dispute is not None


@pytest.mark.asyncio
async def test_late_claim_and_receiver_timeout_are_review_only(session: AsyncSession) -> None:
    receiver, buyer, _, wallet, _, match = await _matched_flow(session)
    p2p = P2PService(session)
    match.payment_deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await session.commit()
    late = await p2p.submit_payment(
        match_id=match.id,
        buyer_user_id=buyer.id,
        provider_namespace="upi",
        claimed_reference="UTR-LATE-CLAIM-001",
        observed_amount_paise=50_000,
        observed_currency="INR",
        declared_paid_at=datetime.now(timezone.utc),
        evidence_upload_id=None,
        provider_evidence_reference=None,
        evidence_metadata=None,
        idempotency_key="p2p-late-claim-001",
        commit=True,
    )
    late_wallet = await session.get(Wallet, wallet.id)
    assert late.match is not None and late.match.status == P2PMatchStatus.UNDER_VERIFICATION
    assert late.response_payload["submission"]["is_late"] is True
    assert late.response_payload["submission"]["verification_status"] == PaymentSubmissionVerificationStatus.MANUAL_REVIEW.value
    assert late_wallet is not None and late_wallet.locked_paise == 50_000

    receiver2, buyer2, _, wallet2, _, match2 = await _matched_flow(session)
    await p2p.submit_payment(
        match_id=match2.id,
        buyer_user_id=buyer2.id,
        provider_namespace="upi",
        claimed_reference="UTR-RECEIVER-TIMEOUT-001",
        observed_amount_paise=50_000,
        observed_currency="INR",
        declared_paid_at=datetime.now(timezone.utc),
        evidence_upload_id=None,
        provider_evidence_reference=None,
        evidence_metadata=None,
        idempotency_key="p2p-receiver-timeout-submit-001",
        commit=True,
    )
    match2.receiver_confirmation_deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await session.commit()
    timed_out = await p2p.expire_match(
        match_id=match2.id,
        idempotency_key="p2p-receiver-timeout-expire-001",
        commit=True,
    )
    timed_out_wallet = await session.get(Wallet, wallet2.id)
    settlements = await session.scalar(select(func.count()).select_from(P2PSettlement))

    assert receiver2.id != buyer2.id
    assert timed_out.match is not None and timed_out.match.status == P2PMatchStatus.UNDER_VERIFICATION
    assert timed_out_wallet is not None and timed_out_wallet.locked_paise == 50_000
    assert settlements == 0


@pytest.mark.asyncio
async def test_wrong_amount_is_review_only_and_no_exact_receiver_does_not_expose_destination(
    session: AsyncSession,
) -> None:
    receiver, buyer, _, _, _, match = await _matched_flow(session)
    p2p = P2PService(session)
    wrong = await p2p.submit_payment(
        match_id=match.id,
        buyer_user_id=buyer.id,
        provider_namespace="upi",
        claimed_reference="UTR-WRONG-AMOUNT-001",
        observed_amount_paise=49_999,
        observed_currency="INR",
        declared_paid_at=datetime.now(timezone.utc),
        evidence_upload_id=None,
        provider_evidence_reference=None,
        evidence_metadata=None,
        idempotency_key="p2p-submit-wrong-001",
        commit=True,
    )
    assert wrong.match is not None and wrong.match.status == P2PMatchStatus.UNDER_VERIFICATION
    settlement = await session.scalar(select(P2PSettlement))
    assert settlement is None
    payment_submission_id = UUID(str(wrong.response_payload["submission"]["id"]))
    with pytest.raises(ConflictError, match="AMOUNT_MISMATCH"):
        await p2p.admin_settle(
            match_id=match.id,
            actor_user_id=(await _user(session, f"extra-admin-{uuid4()}@example.com", admin=True)).id,
            payment_submission_id=payment_submission_id,
            reason="not reached",
            evidence_reference="not-reached",
            verification_source="MANUAL_RECONCILIATION",
            dispute_id=None,
            idempotency_key="p2p-wrong-settle-001",
            commit=True,
        )
    assert receiver.id != buyer.id


@pytest.mark.asyncio
async def test_nonmaximum_request_threshold_override_and_no_exact_match_are_explicit(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    receiver = await _user(session, f"ceiling-receiver-{uuid4()}@example.com")
    receiver_wallet = await _wallet_with_credit(session, receiver, 200_000)
    destination = await _verified_destination(session, receiver)
    small = await WithdrawalService(session).create(
        actor_user_id=receiver.id,
        amount_paise=50_000,
        payment_destination_id=destination.id,
        idempotency_key="p2p-nonmaximum-001",
        commit=True,
    )
    assert small.withdrawal.max_amount_snapshot_paise == 100_000
    assert receiver_wallet.id == small.withdrawal.wallet_id

    low_receiver = await _user(session, f"low-receiver-{uuid4()}@example.com")
    await _wallet_with_credit(session, low_receiver, 30_000)
    low_destination = await _verified_destination(session, low_receiver)
    with pytest.raises(ValidationError, match="INSUFFICIENT_ELIGIBLE_BALANCE"):
        await WithdrawalService(session).create(
            actor_user_id=low_receiver.id,
            amount_paise=25_000,
            payment_destination_id=low_destination.id,
            idempotency_key="p2p-threshold-disabled-001",
            commit=True,
        )
    monkeypatch.setenv("TICKET_P2P_THRESHOLD_OVERRIDE_ENABLED", "true")
    monkeypatch.setenv("TICKET_P2P_THRESHOLD_PAISE", "50000")
    get_settings.cache_clear()
    try:
        allowed = await WithdrawalService(session).create(
            actor_user_id=low_receiver.id,
            amount_paise=25_000,
            payment_destination_id=low_destination.id,
            idempotency_key="p2p-threshold-enabled-001",
            commit=True,
        )
        assert allowed.withdrawal.max_amount_snapshot_paise == 30_000
    finally:
        get_settings.cache_clear()

    buyer = await _user(session, f"no-match-buyer-{uuid4()}@example.com")
    order = await _order(session, buyer, 77_777)
    queued = await P2PService(session).queue_or_match_order(
        order_id=order.id,
        buyer_user_id=buyer.id,
        idempotency_key="p2p-no-exact-match-001",
        commit=True,
    )
    assert queued.match is None
    assert queued.response_payload["match"] is None
    assert queued.response_payload["order"]["status"] == OrderStatus.WAITING_FOR_MATCH.value


@pytest.mark.asyncio
async def test_duplicate_claim_is_reviewed_and_unmatched_cancellation_releases_once(
    session: AsyncSession,
) -> None:
    receiver, buyer, _, _, _, match = await _matched_flow(session)
    _, buyer_two, _, _, _, match_two = await _matched_flow(session)
    p2p = P2PService(session)
    first = await p2p.submit_payment(
        match_id=match.id,
        buyer_user_id=buyer.id,
        provider_namespace="upi",
        claimed_reference="UTR-DUPLICATE-001",
        observed_amount_paise=50_000,
        observed_currency="INR",
        declared_paid_at=datetime.now(timezone.utc),
        evidence_upload_id=None,
        provider_evidence_reference=None,
        evidence_metadata=None,
        idempotency_key="p2p-duplicate-first-001",
        commit=True,
    )
    second = await p2p.submit_payment(
        match_id=match_two.id,
        buyer_user_id=buyer_two.id,
        provider_namespace="upi",
        claimed_reference="UTR-DUPLICATE-001",
        observed_amount_paise=50_000,
        observed_currency="INR",
        declared_paid_at=datetime.now(timezone.utc),
        evidence_upload_id=None,
        provider_evidence_reference=None,
        evidence_metadata=None,
        idempotency_key="p2p-duplicate-second-001",
        commit=True,
    )
    assert first.response_payload["submission"]["verification_status"] == PaymentSubmissionVerificationStatus.UNVERIFIED.value
    assert second.response_payload["submission"]["verification_status"] == PaymentSubmissionVerificationStatus.MANUAL_REVIEW.value

    cancel_receiver = await _user(session, f"cancel-receiver-{uuid4()}@example.com")
    cancel_wallet = await _wallet_with_credit(session, cancel_receiver, 100_000)
    destination = await _verified_destination(session, cancel_receiver)
    withdrawal = await WithdrawalService(session).create(
        actor_user_id=cancel_receiver.id,
        amount_paise=50_000,
        payment_destination_id=destination.id,
        idempotency_key="p2p-cancel-create-001",
        commit=True,
    )
    cancelled = await WithdrawalService(session).cancel(
        withdrawal_id=withdrawal.withdrawal.id,
        actor_user_id=cancel_receiver.id,
        idempotency_key="p2p-cancel-001",
        commit=True,
    )
    refreshed = await session.get(Wallet, cancel_wallet.id)
    assert cancelled.withdrawal.status == WithdrawalStatus.CANCELLED
    assert refreshed is not None and (refreshed.available_paise, refreshed.locked_paise) == (100_000, 0)
    assert receiver.id != buyer.id


@pytest.mark.asyncio
async def test_failed_settlement_rolls_back_wallet_journal_order_and_outbox(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    receiver, buyer, admin, wallet, order, match = await _matched_flow(session)
    wallet_id, order_id, match_id = wallet.id, order.id, match.id
    p2p = P2PService(session)
    submitted = await p2p.submit_payment(
        match_id=match_id,
        buyer_user_id=buyer.id,
        provider_namespace="upi",
        claimed_reference="UTR-ROLLBACK-001",
        observed_amount_paise=50_000,
        observed_currency="INR",
        declared_paid_at=datetime.now(timezone.utc),
        evidence_upload_id=None,
        provider_evidence_reference=None,
        evidence_metadata=None,
        idempotency_key="p2p-rollback-submit-001",
        commit=True,
    )
    posting_count_before = await session.scalar(select(func.count()).select_from(JournalPosting))
    outbox_count_before = await session.scalar(select(func.count()).select_from(P2POutboxEvent))
    original_settle = WalletService.settle_active_hold_for_p2p

    async def fail_after_wallet_write(self: WalletService, **kwargs: object):
        await original_settle(self, **kwargs)
        raise RuntimeError("simulate failure after the journal and hold write")

    monkeypatch.setattr(WalletService, "settle_active_hold_for_p2p", fail_after_wallet_write)
    with pytest.raises(RuntimeError, match="simulate failure"):
        await p2p.admin_settle(
            match_id=match_id,
            actor_user_id=admin.id,
            payment_submission_id=UUID(str(submitted.response_payload["submission"]["id"])),
            reason="This test must roll back every partial write",
            evidence_reference="rollback-case-001",
            verification_source="MANUAL_RECONCILIATION",
            dispute_id=None,
            idempotency_key="p2p-rollback-settle-001",
            commit=True,
        )
    await session.rollback()

    wallet_after = await session.get(Wallet, wallet_id)
    order_after = await session.get(Order, order_id)
    match_after = await session.get(P2PMatch, match_id)
    hold = await session.scalar(select(WalletHold).where(WalletHold.wallet_id == wallet_id))
    settlement_count = await session.scalar(select(func.count()).select_from(P2PSettlement))
    outbox_count = await session.scalar(select(func.count()).select_from(P2POutboxEvent))
    posting_count_after = await session.scalar(select(func.count()).select_from(JournalPosting))

    assert wallet_after is not None and (wallet_after.available_paise, wallet_after.locked_paise) == (50_000, 50_000)
    assert order_after is not None and order_after.status == OrderStatus.AWAITING_PAYMENT
    assert match_after is not None and match_after.status == P2PMatchStatus.WAITING_FOR_RECEIVER_CONFIRMATION
    assert hold is not None and hold.status == WalletHoldStatus.ACTIVE
    assert settlement_count == 0
    assert outbox_count == outbox_count_before
    assert posting_count_after == posting_count_before


@pytest.mark.asyncio
async def test_refund_case_requires_settled_evidence_and_never_restores_wallet(
    session: AsyncSession,
) -> None:
    receiver, buyer, admin, wallet, order, match = await _matched_flow(session)
    p2p = P2PService(session)
    submitted = await p2p.submit_payment(
        match_id=match.id,
        buyer_user_id=buyer.id,
        provider_namespace="upi",
        claimed_reference="UTR-REFUND-CASE-001",
        observed_amount_paise=50_000,
        observed_currency="INR",
        declared_paid_at=datetime.now(timezone.utc),
        evidence_upload_id=None,
        provider_evidence_reference=None,
        evidence_metadata=None,
        idempotency_key="p2p-refund-submit-001",
        commit=True,
    )
    await p2p.admin_settle(
        match_id=match.id,
        actor_user_id=admin.id,
        payment_submission_id=UUID(str(submitted.response_payload["submission"]["id"])),
        reason="Original payment is independently reconciled",
        evidence_reference="refund-case-settlement-evidence-001",
        verification_source="MANUAL_RECONCILIATION",
        dispute_id=None,
        idempotency_key="p2p-refund-settle-001",
        commit=True,
    )
    posting_count_before = await session.scalar(select(func.count()).select_from(JournalPosting))
    created = await RefundService(session).create_pending_case(
        match_id=match.id,
        actor_user_id=admin.id,
        amount_paise=50_000,
        currency="INR",
        reason="Buyer refund requires separately funded review",
        liable_party="platform",
        funding_source_reference="approved-refund-fund-001",
        destination_validation_reference="verified-buyer-destination-001",
        executor_reference="refund-operations-team",
        idempotency_key="p2p-refund-case-001",
        commit=True,
    )
    replay = await RefundService(session).create_pending_case(
        match_id=match.id,
        actor_user_id=admin.id,
        amount_paise=50_000,
        currency="INR",
        reason="Buyer refund requires separately funded review",
        liable_party="platform",
        funding_source_reference="approved-refund-fund-001",
        destination_validation_reference="verified-buyer-destination-001",
        executor_reference="refund-operations-team",
        idempotency_key="p2p-refund-case-001",
        commit=True,
    )
    refreshed_wallet = await session.get(Wallet, wallet.id)
    refreshed_order = await session.get(Order, order.id)
    refreshed_match = await session.get(P2PMatch, match.id)
    refund_count = await session.scalar(select(func.count()).select_from(P2PRefund))
    posting_count_after = await session.scalar(select(func.count()).select_from(JournalPosting))

    assert created.refund.status == P2PRefundStatus.PENDING_EVIDENCE
    assert created.refund.payout_reference is None
    assert created.refund.payout_verified_at is None
    assert replay.replayed
    assert refreshed_wallet is not None and (refreshed_wallet.available_paise, refreshed_wallet.locked_paise) == (50_000, 0)
    assert refreshed_order is not None and refreshed_order.status == OrderStatus.REFUND_PENDING
    assert refreshed_match is not None and refreshed_match.status == P2PMatchStatus.REFUND_PENDING
    assert refund_count == 1
    assert posting_count_after == posting_count_before


@pytest.mark.asyncio
async def test_settled_p2p_order_delivers_one_ticket_without_another_debit(
    session: AsyncSession,
) -> None:
    receiver = await _user(session, f"delivery-success-receiver-{uuid4()}@example.com")
    buyer = await _user(session, f"delivery-success-buyer-{uuid4()}@example.com")
    admin = await _user(session, f"delivery-success-admin-{uuid4()}@example.com", admin=True)
    wallet = await _wallet_with_credit(session, receiver, 100_000)
    destination = await _verified_destination(session, receiver)
    withdrawal = await WithdrawalService(session).create(
        actor_user_id=receiver.id,
        amount_paise=50_000,
        payment_destination_id=destination.id,
        idempotency_key="p2p-delivery-success-withdrawal-001",
        commit=True,
    )
    series = await _open_series_for_p2p(session, admin, price_paise=50_000)
    order = await OrderService(session).create(
        buyer_user_id=buyer.id,
        selection=OrderSelection(TicketProductType.SERIES, series.id, 1),
        idempotency_key="p2p-delivery-success-order-001",
        commit=True,
    )
    p2p = P2PService(session)
    matched = await p2p.queue_or_match_order(
        order_id=order.order.id,
        buyer_user_id=buyer.id,
        idempotency_key="p2p-delivery-success-match-001",
        commit=True,
    )
    assert matched.match is not None
    submitted = await p2p.submit_payment(
        match_id=matched.match.id,
        buyer_user_id=buyer.id,
        provider_namespace="upi",
        claimed_reference="UTR-DELIVERY-SUCCESS-001",
        observed_amount_paise=50_000,
        observed_currency="INR",
        declared_paid_at=datetime.now(timezone.utc),
        evidence_upload_id=None,
        provider_evidence_reference=None,
        evidence_metadata=None,
        idempotency_key="p2p-delivery-success-submit-001",
        commit=True,
    )
    await p2p.admin_settle(
        match_id=matched.match.id,
        actor_user_id=admin.id,
        payment_submission_id=UUID(str(submitted.response_payload["submission"]["id"])),
        reason="Settlement evidence is complete before fulfillment",
        evidence_reference="delivery-success-evidence-001",
        verification_source="MANUAL_RECONCILIATION",
        dispute_id=None,
        idempotency_key="p2p-delivery-success-settle-001",
        commit=True,
    )
    first_delivery = await p2p.retry_delivery(
        order_id=order.order.id,
        actor_user_id=admin.id,
        idempotency_key="p2p-delivery-success-retry-001",
        commit=True,
    )
    replay_delivery = await p2p.retry_delivery(
        order_id=order.order.id,
        actor_user_id=admin.id,
        idempotency_key="p2p-delivery-success-retry-002",
        commit=True,
    )
    tickets = list(await session.scalars(select(Ticket).where(Ticket.owner_user_id == buyer.id)))
    refreshed_wallet = await session.get(Wallet, wallet.id)
    settlements = await session.scalar(select(func.count()).select_from(P2PSettlement))

    assert withdrawal.withdrawal.status == WithdrawalStatus.COMPLETED
    assert first_delivery["status"] == OrderStatus.FULFILLED.value
    assert replay_delivery["status"] == OrderStatus.FULFILLED.value
    assert len(tickets) == 1 and tickets[0].serial_number == 1
    assert refreshed_wallet is not None and (refreshed_wallet.available_paise, refreshed_wallet.locked_paise) == (50_000, 0)
    assert settlements == 1


@pytest.mark.asyncio
async def test_delivery_failure_keeps_paid_settlement_and_never_debits_again(session: AsyncSession) -> None:
    receiver, buyer, admin, wallet, order, match = await _matched_flow(session)
    wallet_id = wallet.id
    order_id = order.id
    p2p = P2PService(session)
    submitted = await p2p.submit_payment(
        match_id=match.id,
        buyer_user_id=buyer.id,
        provider_namespace="upi",
        claimed_reference="UTR-DELIVERY-001",
        observed_amount_paise=50_000,
        observed_currency="INR",
        declared_paid_at=datetime.now(timezone.utc),
        evidence_upload_id=None,
        provider_evidence_reference=None,
        evidence_metadata=None,
        idempotency_key="p2p-delivery-submit-001",
        commit=True,
    )
    await p2p.admin_settle(
        match_id=match.id,
        actor_user_id=admin.id,
        payment_submission_id=UUID(str(submitted.response_payload["submission"]["id"])),
        reason="Evidence supports settlement before delivery",
        evidence_reference="delivery-failure-case-001",
        verification_source="MANUAL_RECONCILIATION",
        dispute_id=None,
        idempotency_key="p2p-delivery-settle-001",
        commit=True,
    )
    with pytest.raises(ConflictError):
        await p2p.retry_delivery(
            order_id=order_id,
            actor_user_id=admin.id,
            idempotency_key="p2p-delivery-retry-001",
            commit=True,
        )
    with pytest.raises(ConflictError):
        await p2p.retry_delivery(
            order_id=order_id,
            actor_user_id=admin.id,
            idempotency_key="p2p-delivery-retry-001",
            commit=True,
        )
    wallet_after = await session.get(Wallet, wallet_id)
    order_after = await session.get(Order, order_id)
    settlements = await session.scalar(select(func.count()).select_from(P2PSettlement))
    failed_retry_records = await session.scalar(
        select(func.count())
        .select_from(IdempotencyRecord)
        .where(
            IdempotencyRecord.actor_scope == f"admin:{admin.id}:p2p.delivery-retry",
            IdempotencyRecord.idempotency_key == "p2p-delivery-retry-001",
        )
    )
    assert wallet_after is not None and (wallet_after.available_paise, wallet_after.locked_paise) == (50_000, 0)
    assert order_after is not None and (order_after.status, order_after.delivery_status) == (OrderStatus.PAID, DeliveryStatus.FAILED)
    assert settlements == 1
    assert failed_retry_records == 1


@pytest.mark.asyncio
async def test_signed_provider_event_is_durable_and_settles_via_confirmation_outbox(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    receiver, buyer, _admin, _wallet, order, match = await _matched_flow(session)
    p2p = P2PService(session)
    submitted = await p2p.submit_payment(
        match_id=match.id,
        buyer_user_id=buyer.id,
        provider_namespace="upi",
        claimed_reference="UTR-PROVIDER-OUTBOX-001",
        observed_amount_paise=50_000,
        observed_currency="INR",
        declared_paid_at=datetime.now(timezone.utc),
        evidence_upload_id=None,
        provider_evidence_reference=None,
        evidence_metadata=None,
        idempotency_key="p2p-provider-submit-001",
        commit=True,
    )
    submission_id = UUID(str(submitted.response_payload["submission"]["id"]))
    secret = "provider-webhook-test-secret-0001"
    monkeypatch.setenv("TICKET_P2P_PROVIDER_WEBHOOK_SECRET", secret)
    get_settings.cache_clear()
    try:
        payload = P2PProviderWebhookRequest(
            provider_namespace="upi",
            external_event_id="provider-event-settlement-001",
            event_type="PAYMENT_VERIFIED",
            match_id=match.id,
            payment_submission_id=submission_id,
            transaction_reference="UTR-PROVIDER-OUTBOX-001",
            verified_amount_paise=50_000,
            verified_currency="INR",
            recipient_fingerprint=canonical_payload_digest(match.destination_snapshot["recipient"]),
            occurred_at=datetime.now(timezone.utc),
        )
        raw_body = payload.model_dump_json().encode("utf-8")
        signature = hmac.new(secret.encode("utf-8"), raw_body, sha256).hexdigest()
        first_event = await ProviderEventService(session).ingest_signed_event(
            payload=payload,
            raw_body=raw_body,
            signature=f"sha256={signature}",
            commit=True,
        )
        assert first_event.event.status == P2PProviderEventStatus.PROCESSED
        stored_submission = await session.get(P2PPaymentSubmission, submission_id)
        assert stored_submission is not None
        assert stored_submission.verification_status == PaymentSubmissionVerificationStatus.VERIFIED

        replay_event = await ProviderEventService(session).ingest_signed_event(
            payload=payload,
            raw_body=raw_body,
            signature=signature,
            commit=True,
        )
        assert replay_event.replayed is True
        assert await session.scalar(select(func.count()).select_from(P2PProviderEvent)) == 1

        await p2p.confirm_receipt(
            match_id=match.id,
            receiver_user_id=receiver.id,
            decision=ReceiverConfirmationDecision.RECEIVED,
            reason=None,
            idempotency_key="p2p-provider-confirm-001",
            commit=True,
        )
        settlement_event = await session.scalar(
            select(P2POutboxEvent).where(P2POutboxEvent.event_type == "P2P_SUPPORTED_SETTLEMENT_REQUESTED")
        )
        assert settlement_event is not None
        assert await P2POutboxService(session).dispatch_event(event_id=settlement_event.id, commit=True)
        settlement_count = await session.scalar(select(func.count()).select_from(P2PSettlement))
        order_after = await session.get(Order, order.id)
        refreshed_event = await session.get(P2POutboxEvent, settlement_event.id)
        assert settlement_count == 1
        assert order_after is not None and order_after.status == OrderStatus.PAID
        assert refreshed_event is not None and refreshed_event.status == OutboxEventStatus.DELIVERED
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_signed_unknown_provider_event_is_retained_for_reconciliation(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "provider-webhook-unknown-event-secret-0001"
    monkeypatch.setenv("TICKET_P2P_PROVIDER_WEBHOOK_SECRET", secret)
    get_settings.cache_clear()
    try:
        payload = P2PProviderWebhookRequest(
            provider_namespace="upi",
            external_event_id="provider-event-unknown-001",
            event_type="PAYMENT_VERIFIED",
            match_id=uuid4(),
            payment_submission_id=uuid4(),
            transaction_reference="UTR-PROVIDER-UNKNOWN-001",
            verified_amount_paise=50_000,
            verified_currency="INR",
            recipient_fingerprint="0" * 64,
            occurred_at=datetime.now(timezone.utc),
        )
        raw_body = payload.model_dump_json().encode("utf-8")
        signature = hmac.new(secret.encode("utf-8"), raw_body, sha256).hexdigest()
        result = await ProviderEventService(session).ingest_signed_event(
            payload=payload,
            raw_body=raw_body,
            signature=signature,
            commit=True,
        )
        stored = await session.get(P2PProviderEvent, result.event.id)
        assert result.response_payload["review_required"] is True
        assert stored is not None
        assert stored.status == P2PProviderEventStatus.REVIEW_REQUIRED
        assert stored.match_id is None and stored.payment_submission_id is None
        assert stored.processing_error is not None and stored.processing_error.startswith("UNKNOWN_MATCH")
        assert stored.payload["match_id"] == str(payload.match_id)
        assert stored.payload["payment_submission_id"] == str(payload.payment_submission_id)
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_reference_conflict_persists_review_and_replays_without_a_settlement(session: AsyncSession) -> None:
    receiver_one, buyer_one, _admin_one, _wallet_one, _order_one, match_one = await _matched_flow(session)
    receiver_two, buyer_two, admin_two, _wallet_two, _order_two, match_two = await _matched_flow(session)
    p2p = P2PService(session)
    first = await p2p.submit_payment(
        match_id=match_one.id,
        buyer_user_id=buyer_one.id,
        provider_namespace="upi",
        claimed_reference="UTR-DURABLE-CONFLICT-001",
        observed_amount_paise=50_000,
        observed_currency="INR",
        declared_paid_at=datetime.now(timezone.utc),
        evidence_upload_id=None,
        provider_evidence_reference=None,
        evidence_metadata=None,
        idempotency_key="p2p-conflict-first-submit-001",
        commit=True,
    )
    second = await p2p.submit_payment(
        match_id=match_two.id,
        buyer_user_id=buyer_two.id,
        provider_namespace="upi",
        claimed_reference="UTR-DURABLE-CONFLICT-001",
        observed_amount_paise=50_000,
        observed_currency="INR",
        declared_paid_at=datetime.now(timezone.utc),
        evidence_upload_id=None,
        provider_evidence_reference=None,
        evidence_metadata=None,
        idempotency_key="p2p-conflict-second-submit-001",
        commit=True,
    )
    first_submission_id = UUID(str(first.response_payload["submission"]["id"]))
    second_submission_id = UUID(str(second.response_payload["submission"]["id"]))
    session.add(
        P2PVerifiedPaymentReference(
            id=uuid4(),
            payment_submission_id=first_submission_id,
            match_id=match_one.id,
            provider_namespace="upi",
            transaction_reference="UTR-DURABLE-CONFLICT-001",
            verification_source="TEST_VERIFICATION",
        )
    )
    await session.commit()

    result = await p2p.admin_settle(
        match_id=match_two.id,
        actor_user_id=admin_two.id,
        payment_submission_id=second_submission_id,
        reason="Reference belongs to a different immutable match",
        evidence_reference="durable-reference-conflict-001",
        verification_source="MANUAL_RECONCILIATION",
        dispute_id=None,
        idempotency_key="p2p-conflict-settle-001",
        commit=True,
    )
    replay = await p2p.admin_settle(
        match_id=match_two.id,
        actor_user_id=admin_two.id,
        payment_submission_id=second_submission_id,
        reason="Reference belongs to a different immutable match",
        evidence_reference="durable-reference-conflict-001",
        verification_source="MANUAL_RECONCILIATION",
        dispute_id=None,
        idempotency_key="p2p-conflict-settle-001",
        commit=True,
    )
    refreshed_match = await session.get(P2PMatch, match_two.id)
    refreshed_submission = await session.get(P2PPaymentSubmission, second_submission_id)
    settlements = await session.scalar(select(func.count()).select_from(P2PSettlement))
    conflict_audits = await session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.action == "P2P_MATCH_REFERENCE_CONFLICT_REVIEW_REQUIRED")
    )
    review_events = list(
        await session.scalars(
            select(P2POutboxEvent).where(
                P2POutboxEvent.aggregate_id == match_two.id,
                P2POutboxEvent.event_type == "P2P_NOTIFICATION_REQUESTED",
            )
        )
    )
    assert result.response_payload["code"] == "REFERENCE_CONFLICT"
    assert replay.replayed is True
    assert refreshed_match is not None and refreshed_match.status == P2PMatchStatus.ADMIN_REVIEW
    assert refreshed_submission is not None
    assert refreshed_submission.verification_status == PaymentSubmissionVerificationStatus.MANUAL_REVIEW
    assert settlements == 0
    assert conflict_audits == 1
    assert any(event.payload.get("notification_type") == "P2P_REVIEW_REQUIRED" for event in review_events)


@pytest.mark.asyncio
async def test_dispute_transitions_and_audits_are_server_enforced(session: AsyncSession) -> None:
    receiver, buyer, admin, _wallet, _order, match = await _matched_flow(session)
    p2p = P2PService(session)
    await p2p.submit_payment(
        match_id=match.id,
        buyer_user_id=buyer.id,
        provider_namespace="upi",
        claimed_reference="UTR-DISPUTE-AUDIT-001",
        observed_amount_paise=50_000,
        observed_currency="INR",
        declared_paid_at=datetime.now(timezone.utc),
        evidence_upload_id=None,
        provider_evidence_reference=None,
        evidence_metadata=None,
        idempotency_key="p2p-dispute-audit-submit-001",
        commit=True,
    )
    await p2p.confirm_receipt(
        match_id=match.id,
        receiver_user_id=receiver.id,
        decision=ReceiverConfirmationDecision.NOT_RECEIVED,
        reason="Receiver could not find the transfer",
        idempotency_key="p2p-dispute-audit-confirm-001",
        commit=True,
    )
    dispute = await session.scalar(select(P2PDispute).where(P2PDispute.match_id == match.id))
    assert dispute is not None and dispute.status == P2PDisputeStatus.OPEN
    await DisputeService(session).resolve(
        dispute_id=dispute.id,
        actor_user_id=admin.id,
        decision=AdminResolutionDecision.KEEP_IN_REVIEW,
        payment_submission_id=None,
        reason="Independent reconciliation remains pending",
        evidence_reference="dispute-audit-evidence-001",
        verification_source="MANUAL_RECONCILIATION",
        release_hold=False,
        idempotency_key="p2p-dispute-audit-resolve-001",
        commit=True,
    )
    refreshed = await session.get(P2PDispute, dispute.id)
    actions = set(
        (
            await session.scalars(
                select(AuditLog.action).where(AuditLog.entity_type == "p2p_dispute")
            )
        ).all()
    )
    assert refreshed is not None and refreshed.status == P2PDisputeStatus.UNDER_REVIEW
    assert {"P2P_DISPUTE_OPENED", "P2P_DISPUTE_UNDER_ADMIN_REVIEW"} <= actions


@pytest.mark.asyncio
async def test_notification_outbox_dispatches_durable_in_app_notices(session: AsyncSession) -> None:
    _receiver, _buyer, _admin, _wallet, _order, match = await _matched_flow(session)
    event = await session.scalar(
        select(P2POutboxEvent)
        .where(
            P2POutboxEvent.aggregate_id == match.id,
            P2POutboxEvent.event_type == "P2P_NOTIFICATION_REQUESTED",
        )
        .order_by(P2POutboxEvent.created_at)
    )
    assert event is not None
    assert await P2POutboxService(session).dispatch_event(event_id=event.id, commit=True)
    notifications = await session.scalar(
        select(func.count()).select_from(P2PNotification).where(P2PNotification.outbox_event_id == event.id)
    )
    refreshed_event = await session.get(P2POutboxEvent, event.id)
    assert notifications == 2
    assert refreshed_event is not None and refreshed_event.status == OutboxEventStatus.DELIVERED


@pytest.mark.asyncio
async def test_configured_verified_user_policy_blocks_unverified_p2p_access(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _user(session, f"p2p-risk-{uuid4()}@example.com")
    monkeypatch.setenv("TICKET_P2P_REQUIRE_VERIFIED_USERS", "true")
    get_settings.cache_clear()
    try:
        with pytest.raises(ConflictError, match="P2P_RISK_REVIEW_REQUIRED"):
            await P2PRiskService(session).assert_eligible(user.id)
        user.is_verified = True
        await session.commit()
        assert (await P2PRiskService(session).assert_eligible(user.id)).id == user.id
    finally:
        get_settings.cache_clear()
