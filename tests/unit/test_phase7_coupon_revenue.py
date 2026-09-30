"""Phase 7 coupon snapshots and immutable revenue-allocation coverage."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import RoleName
from app.exceptions import ConflictError
from app.models.coupon import Coupon, CouponDiscountType, CouponRedemption, CouponRedemptionStatus
from app.models.ledger import JournalPosting
from app.models.order import DeliveryStatus, Order, OrderStatus, TicketProductType
from app.models.revenue_allocation import RevenueAllocation, RevenueAllocationSource
from app.models.ticket_series import TicketSeries, TicketSeriesStatus
from app.models.user import Role, User
from app.services.coupon_service import CouponService
from app.services.ledger_service import LedgerService
from app.services.order_service import OrderSelection, OrderService
from app.services.revenue_service import RevenueService


async def _user(session: AsyncSession, *, admin: bool = False) -> User:
    roles = [Role(name=RoleName.ADMIN.value)] if admin else []
    user = User(
        email=f"phase7-{uuid4()}@example.test",
        password_hash="test-only-hash",
        roles=roles,
    )
    session.add(user)
    await session.commit()
    return user


async def _open_series(session: AsyncSession, *, admin: User, price_paise: int = 3_000) -> TicketSeries:
    now = datetime.now(timezone.utc)
    series = TicketSeries(
        name="Phase 7 Coupon Series",
        description="Internal coupon test catalog row",
        price_paise=price_paise,
        ticket_limit=10,
        sold_count=0,
        reserved_count=0,
        currency="INR",
        sales_start_at=now - timedelta(minutes=1),
        sales_end_at=now + timedelta(hours=1),
        draw_at=now + timedelta(days=1),
        status=TicketSeriesStatus.OPEN,
        created_by_user_id=admin.id,
    )
    session.add(series)
    await session.commit()
    return series


@pytest.mark.asyncio
async def test_coupon_price_snapshot_releases_on_cancel_and_can_be_reused(session: AsyncSession) -> None:
    admin = await _user(session, admin=True)
    buyer = await _user(session)
    series = await _open_series(session, admin=admin)
    coupons = CouponService(session)
    created = await coupons.create(
        actor_user_id=admin.id,
        code="save-500",
        description="Fixed launch discount",
        discount_type=CouponDiscountType.FIXED_PAISE,
        fixed_discount_paise=500,
        percentage_bps=None,
        max_discount_paise=None,
        minimum_order_paise=1_000,
        usage_limit=1,
        per_user_limit=1,
        starts_at=None,
        ends_at=None,
        idempotency_key="phase7-coupon-create-001",
        commit=True,
    )

    orders = OrderService(session)
    first = await orders.create(
        buyer_user_id=buyer.id,
        selection=OrderSelection(TicketProductType.SERIES, series.id, 1, " save-500 "),
        idempotency_key="phase7-order-create-001",
        commit=True,
    )
    first_order_id = first.order.id
    assert (
        first.order.subtotal_paise,
        first.order.discount_paise,
        first.order.total_paise,
        first.order.coupon_code_snapshot,
    ) == (3_000, 500, 2_500, "SAVE-500")
    redemption = await session.scalar(
        select(CouponRedemption).where(CouponRedemption.order_id == first_order_id)
    )
    assert redemption is not None and redemption.status == CouponRedemptionStatus.RESERVED
    assert redemption.rule_snapshot["fixed_discount_paise"] == 500
    coupon = await session.get(Coupon, created.coupon.id)
    assert coupon is not None and coupon.active_redemption_count == 1

    replay = await orders.create(
        buyer_user_id=buyer.id,
        selection=OrderSelection(TicketProductType.SERIES, series.id, 1, "SAVE-500"),
        idempotency_key="phase7-order-create-001",
        commit=True,
    )
    assert replay.replayed and replay.order.id == first_order_id

    cancelled = await orders.cancel(
        order_id=first_order_id,
        actor_user_id=buyer.id,
        idempotency_key="phase7-order-cancel-001",
        commit=True,
    )
    assert cancelled.order.status == OrderStatus.CANCELLED
    await session.refresh(redemption)
    await session.refresh(coupon)
    assert redemption.status == CouponRedemptionStatus.RELEASED
    assert coupon.active_redemption_count == 0

    reused = await orders.create(
        buyer_user_id=buyer.id,
        selection=OrderSelection(TicketProductType.SERIES, series.id, 1, "SAVE-500"),
        idempotency_key="phase7-order-create-002",
        commit=True,
    )
    assert reused.order.total_paise == 2_500
    assert await session.scalar(select(func.count()).select_from(CouponRedemption)) == 2


@pytest.mark.asyncio
async def test_coupon_cannot_consume_without_a_trusted_settlement(session: AsyncSession) -> None:
    admin = await _user(session, admin=True)
    buyer = await _user(session)
    series = await _open_series(session, admin=admin)
    coupons = CouponService(session)
    await coupons.create(
        actor_user_id=admin.id,
        code="SAFE100",
        description=None,
        discount_type=CouponDiscountType.FIXED_PAISE,
        fixed_discount_paise=100,
        percentage_bps=None,
        max_discount_paise=None,
        minimum_order_paise=0,
        usage_limit=None,
        per_user_limit=None,
        starts_at=None,
        ends_at=None,
        idempotency_key="phase7-coupon-create-002",
        commit=True,
    )
    order = (
        await OrderService(session).create(
            buyer_user_id=buyer.id,
            selection=OrderSelection(TicketProductType.SERIES, series.id, 1, "safe100"),
            idempotency_key="phase7-order-create-003",
            commit=True,
        )
    ).order
    with pytest.raises(ConflictError, match="ORDER_NOT_SETTLED"):
        await coupons.consume_for_settled_order(order=order, actor_user_id=admin.id)
    await session.rollback()


@pytest.mark.asyncio
async def test_revenue_split_is_idempotent_balanced_and_remainder_goes_to_profit(session: AsyncSession) -> None:
    buyer = await _user(session)
    settlement_id = uuid4()
    now = datetime.now(timezone.utc)
    order = Order(
        buyer_user_id=buyer.id,
        status=OrderStatus.PAID,
        delivery_status=DeliveryStatus.PENDING,
        subtotal_paise=10_003,
        discount_paise=0,
        total_paise=10_003,
        currency="INR",
        expires_at=now + timedelta(minutes=15),
        settlement_reference_id=settlement_id,
        settled_at=now,
    )
    session.add(order)
    await session.commit()

    revenue = RevenueService(session)
    first = await revenue.allocate_settled_order(
        order=order,
        settlement_reference_id=settlement_id,
        source=RevenueAllocationSource.EXTERNAL_ORDER_PENDING,
        actor_user_id=None,
        commit=True,
    )
    allocation = first.allocation
    assert (
        allocation.prize_pool_paise,
        allocation.marketing_paise,
        allocation.operations_paise,
        allocation.reserve_paise,
        allocation.profit_growth_paise,
    ) == (4_501, 2_000, 1_000, 500, 2_002)
    assert sum(
        (
            allocation.prize_pool_paise,
            allocation.marketing_paise,
            allocation.operations_paise,
            allocation.reserve_paise,
            allocation.profit_growth_paise,
        )
    ) == allocation.allocation_base_paise
    await LedgerService(session).assert_group_balanced(allocation.journal_group_id)
    assert (
        await session.scalar(
            select(func.count()).select_from(JournalPosting).where(
                JournalPosting.journal_group_id == allocation.journal_group_id
            )
        )
    ) == 6

    replay = await revenue.allocate_settled_order(
        order=order,
        settlement_reference_id=settlement_id,
        source=RevenueAllocationSource.EXTERNAL_ORDER_PENDING,
        actor_user_id=None,
        commit=True,
    )
    assert replay.replayed and replay.allocation.id == allocation.id
    assert await session.scalar(select(func.count()).select_from(RevenueAllocation)) == 1
    report = await revenue.report()
    assert report["allocation_count"] == 1
    assert report["allocation_base_paise"] == 10_003
    assert report["profit_growth_paise"] == 2_002
