"""Phase 7 marketing eligibility and non-wallet reward evidence."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import RoleName
from app.exceptions import ConflictError
from app.models.affiliate import AffiliateCommission, AffiliateCommissionType
from app.models.cashback import CashbackReward, CashbackRewardType
from app.models.ledger import JournalGroup
from app.models.order import DeliveryStatus, Order, OrderStatus
from app.models.referral import ReferralReward, ReferralRewardStatus
from app.models.user import Role, User
from app.services.affiliate_service import AffiliateService
from app.services.cashback_service import CashbackService
from app.services.referral_service import ReferralService


async def _user(session: AsyncSession, *, admin: bool = False) -> User:
    user = User(
        email=f"rewards-{uuid4()}@example.test",
        password_hash="test-only-hash",
        roles=[Role(name=RoleName.ADMIN.value)] if admin else [],
    )
    session.add(user)
    await session.commit()
    return user


async def _settled_order(session: AsyncSession, buyer: User, amount: int = 3_000) -> Order:
    now = datetime.now(timezone.utc)
    order = Order(
        buyer_user_id=buyer.id, status=OrderStatus.PAID,
        delivery_status=DeliveryStatus.PENDING,
        subtotal_paise=amount, discount_paise=0, total_paise=amount,
        currency="INR", expires_at=now + timedelta(minutes=15),
        settlement_reference_id=uuid4(), settled_at=now,
    )
    session.add(order)
    await session.commit()
    return order


@pytest.mark.asyncio
async def test_referral_is_claimed_before_first_settlement_and_rewards_stay_pending(session: AsyncSession) -> None:
    admin = await _user(session, admin=True)
    referrer = await _user(session)
    referred = await _user(session)
    service = ReferralService(session)
    created = await service.create_program(
        actor_user_id=admin.id, name="First order", description=None,
        referrer_reward_paise=120, referred_reward_paise=80,
        minimum_order_paise=1_000, starts_at=None, ends_at=None,
        idempotency_key="referral-program-001", commit=True,
    )
    await service.activate_program(
        program_id=created.resource.id, actor_user_id=admin.id,
        idempotency_key="referral-activate-001", commit=True,
    )
    program_id = created.resource.id
    admin_id = admin.id
    profile = await service.create_profile(
        user_id=referrer.id, idempotency_key="referral-profile-001", commit=True
    )
    referred_id = referred.id
    referral_code = profile.resource.code
    with pytest.raises(ConflictError, match="SELF_REFERRAL_FORBIDDEN"):
        await service.claim(
            referred_user_id=referrer.id, referral_code=referral_code,
            idempotency_key="referral-self-001", commit=True,
        )
    await session.rollback()
    claimed = await service.claim(
        referred_user_id=referred_id, referral_code=referral_code,
        idempotency_key="referral-claim-001", commit=True,
    )
    assert claimed.resource.program_snapshot["program_id"] == str(program_id)
    referred = await session.get(User, referred_id)
    order = await _settled_order(session, referred)
    rewards = await service.qualify_settled_order(
        order=order, settlement_reference_id=order.settlement_reference_id,
        actor_user_id=admin_id,
    )
    await session.commit()
    assert sorted(reward.amount_paise for reward in rewards) == [80, 120]
    assert all(reward.status == ReferralRewardStatus.PENDING_REVIEW for reward in rewards)
    assert await session.scalar(select(func.count()).select_from(ReferralReward)) == 2
    assert await session.scalar(select(func.count()).select_from(JournalGroup)) == 0
    assert await service.qualify_settled_order(
        order=order, settlement_reference_id=order.settlement_reference_id,
        actor_user_id=admin_id,
    ) == []
    voided = await service.void_reward(
        reward_id=rewards[0].id, actor_user_id=admin_id,
        reason="Duplicate attribution reviewed", idempotency_key="referral-void-001",
        commit=True,
    )
    assert voided["status"] == "VOIDED"
    disabled = await service.disable_program(
        program_id=program_id, actor_user_id=admin_id,
        reason="Campaign rotation", idempotency_key="referral-disable-001",
        commit=True,
    )
    assert disabled.response_payload["status"] == "DISABLED"
    with pytest.raises(ConflictError, match="REFERRAL_PROGRAM_DISABLED"):
        await service.activate_program(
            program_id=program_id, actor_user_id=admin_id,
            idempotency_key="referral-reactivate-001", commit=True,
        )
    await session.rollback()


@pytest.mark.asyncio
async def test_cashback_only_after_settlement_and_replay_is_same_record(session: AsyncSession) -> None:
    admin = await _user(session, admin=True)
    buyer = await _user(session)
    service = CashbackService(session)
    campaign, _, _ = await service.create_campaign(
        actor_user_id=admin.id, code="BACK10", name="Ten percent", description=None,
        reward_type=CashbackRewardType.PERCENT_BPS, fixed_reward_paise=None,
        percentage_bps=1_000, max_reward_paise=250,
        minimum_order_paise=1_000, starts_at=None, ends_at=None,
        idempotency_key="cashback-create-001", commit=True,
    )
    await service.activate_campaign(
        campaign_id=campaign.id, actor_user_id=admin.id,
        idempotency_key="cashback-activate-001", commit=True,
    )
    pending = Order(
        buyer_user_id=buyer.id, status=OrderStatus.PENDING_PAYMENT,
        delivery_status=DeliveryStatus.NOT_STARTED,
        subtotal_paise=3_000, discount_paise=0, total_paise=3_000,
        currency="INR", expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
    )
    session.add(pending)
    await session.commit()
    buyer_id = buyer.id
    admin_id = admin.id
    with pytest.raises(ConflictError, match="ORDER_NOT_SETTLED"):
        await service.create_pending_reward_for_settled_order(
            order=pending, settlement_reference_id=uuid4(), actor_user_id=admin.id
        )
    await session.rollback()
    buyer = await session.get(User, buyer_id)
    order = await _settled_order(session, buyer)
    first = await service.create_pending_reward_for_settled_order(
        order=order, settlement_reference_id=order.settlement_reference_id,
        actor_user_id=admin_id,
    )
    await session.commit()
    second = await service.create_pending_reward_for_settled_order(
        order=order, settlement_reference_id=order.settlement_reference_id,
        actor_user_id=admin_id,
    )
    assert first.id == second.id
    assert first.amount_paise == 250
    assert await session.scalar(select(func.count()).select_from(CashbackReward)) == 1
    assert await session.scalar(select(func.count()).select_from(JournalGroup)) == 0
    voided = await service.void_reward(
        reward_id=first.id, actor_user_id=admin_id, reason="Fraud review",
        idempotency_key="cashback-void-001", commit=True,
    )
    assert voided["status"] == "VOIDED"
    disabled = await service.disable_campaign(
        campaign_id=campaign.id, actor_user_id=admin_id,
        reason="Campaign ended", idempotency_key="cashback-disable-001",
        commit=True,
    )
    assert disabled["status"] == "DISABLED"


@pytest.mark.asyncio
async def test_affiliate_conversion_requires_evidence_and_blocks_own_order(session: AsyncSession) -> None:
    admin = await _user(session, admin=True)
    owner = await _user(session)
    buyer = await _user(session)
    service = AffiliateService(session)
    affiliate, _, _ = await service.create(
        actor_user_id=admin.id, code="PARTNER1", display_name="Partner One",
        owner_user_id=owner.id, commission_type=AffiliateCommissionType.FIXED_PAISE,
        fixed_commission_paise=150, percentage_bps=None, max_commission_paise=None,
        minimum_order_paise=1_000, idempotency_key="affiliate-create-001", commit=True,
    )
    same_affiliate, _, replayed = await service.create(
        actor_user_id=admin.id, code="PARTNER1", display_name="Partner One",
        owner_user_id=owner.id, commission_type=AffiliateCommissionType.FIXED_PAISE,
        fixed_commission_paise=150, percentage_bps=None, max_commission_paise=None,
        minimum_order_paise=1_000, idempotency_key="affiliate-create-001", commit=True,
    )
    assert replayed and same_affiliate.id == affiliate.id
    await service.activate(
        affiliate_id=affiliate.id, actor_user_id=admin.id,
        idempotency_key="affiliate-activate-001", commit=True,
    )
    own_order = await _settled_order(session, owner)
    buyer_id = buyer.id
    affiliate_id = affiliate.id
    admin_id = admin.id
    with pytest.raises(ConflictError, match="SELF_AFFILIATE_FORBIDDEN"):
        await service.attribute_settled_order(
            affiliate_id=affiliate.id, order_id=own_order.id,
            evidence_reference="signed-campaign-report/own",
            actor_user_id=admin.id, idempotency_key="affiliate-own-001", commit=True,
        )
    await session.rollback()
    buyer = await session.get(User, buyer_id)
    order = await _settled_order(session, buyer)
    conversion, commission, _, replayed = await service.attribute_settled_order(
        affiliate_id=affiliate_id, order_id=order.id,
        evidence_reference="signed-campaign-report/2026-09-25",
        actor_user_id=admin_id, idempotency_key="affiliate-conversion-001", commit=True,
    )
    assert not replayed
    assert commission.amount_paise == 150
    assert conversion.created_from_settlement_id == order.settlement_reference_id
    same, same_commission, _, replayed = await service.attribute_settled_order(
        affiliate_id=affiliate_id, order_id=order.id,
        evidence_reference="signed-campaign-report/2026-09-25",
        actor_user_id=admin_id, idempotency_key="affiliate-conversion-001", commit=True,
    )
    assert replayed and same.id == conversion.id and same_commission.id == commission.id
    assert await session.scalar(select(func.count()).select_from(AffiliateCommission)) == 1
    assert await session.scalar(select(func.count()).select_from(JournalGroup)) == 0
    voided = await service.void_conversion(
        conversion_id=conversion.id, actor_user_id=admin_id,
        reason="Attribution evidence rejected",
        idempotency_key="affiliate-void-001", commit=True,
    )
    assert voided["status"] == "VOIDED" and voided["commission"]["status"] == "VOIDED"
    suspended = await service.suspend(
        affiliate_id=affiliate_id, actor_user_id=admin_id,
        reason="Partner review", idempotency_key="affiliate-suspend-001",
        commit=True,
    )
    assert suspended["status"] == "SUSPENDED"
