"""Focused Phase 3 catalog, reservation, and settlement-gated allocation tests."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import RoleName
from app.exceptions import ConflictError
from app.models.order import Order, OrderStatus, TicketProductType
from app.models.ticket import Ticket
from app.models.ticket_package import TicketPackage
from app.models.ticket_series import TicketSeries, TicketSeriesStatus
from app.models.user import Role, User
from app.services.order_service import OrderSelection, OrderService
from app.services.ticket_allocation_service import TicketAllocationService
from app.services.ticket_package_service import PackageItemDraft, TicketPackageService
from app.services.ticket_series_service import PrizeDraft, TicketSeriesService
from app.services.winner_service import WinnerService


async def _admin(session: AsyncSession) -> User:
    role = Role(name=RoleName.ADMIN.value, description="Catalog administrator")
    user = User(
        email=f"admin-{uuid4()}@example.test",
        password_hash="test-only-hash",
        full_name="Admin",
        roles=[role],
    )
    session.add(user)
    await session.commit()
    return user


async def _buyer(session: AsyncSession) -> User:
    user = User(
        email=f"buyer-{uuid4()}@example.test",
        password_hash="test-only-hash",
        full_name="Buyer",
    )
    session.add(user)
    await session.commit()
    return user


async def _open_series(
    session: AsyncSession, admin: User, *, name: str, ticket_limit: int = 10
) -> TicketSeries:
    now = datetime.now(timezone.utc)
    service = TicketSeriesService(session)
    created = await service.create(
        actor_user_id=admin.id,
        name=name,
        description="A limited test series",
        price_paise=3_000,
        ticket_limit=ticket_limit,
        sales_start_at=now - timedelta(minutes=1),
        sales_end_at=now + timedelta(hours=1),
        draw_at=now + timedelta(days=1),
        prizes=[PrizeDraft(rank=1, title="First prize", prize_paise=10_000)],
        idempotency_key=f"series-create-{uuid4()}",
        commit=True,
    )
    await service.publish(
        series_id=created.series.id,
        actor_user_id=admin.id,
        idempotency_key=f"series-publish-{uuid4()}",
        commit=True,
    )
    await WinnerService(session).commit_seed(
        series_id=created.series.id,
        actor_user_id=admin.id,
        seed_commitment=sha256(f"series-draw-seed-{created.series.id}".encode("utf-8")).hexdigest(),
        idempotency_key=f"series-draw-commit-{uuid4()}",
        commit=True,
    )
    opened = await service.open(
        series_id=created.series.id,
        actor_user_id=admin.id,
        idempotency_key=f"series-open-{uuid4()}",
        commit=True,
    )
    return opened.series


@pytest.mark.asyncio
async def test_series_catalog_lifecycle_is_admin_only_and_idempotent(session: AsyncSession) -> None:
    admin = await _admin(session)
    now = datetime.now(timezone.utc)
    service = TicketSeriesService(session)
    kwargs = {
        "actor_user_id": admin.id,
        "name": "Monsoon Draw",
        "description": "Limited series",
        "price_paise": 3_000,
        "ticket_limit": 3,
        "sales_start_at": now - timedelta(minutes=1),
        "sales_end_at": now + timedelta(hours=1),
        "draw_at": now + timedelta(days=1),
        "prizes": [PrizeDraft(rank=1, title="Top prize", prize_paise=5_000)],
        "idempotency_key": "series-create-test-key",
        "commit": True,
    }
    first = await service.create(**kwargs)
    replay = await service.create(**kwargs)
    series_id = first.series.id
    admin_id = admin.id

    assert first.series.status == TicketSeriesStatus.DRAFT
    assert replay.replayed is True
    assert replay.response_payload == first.response_payload
    assert len((await session.scalars(select(TicketSeries)) ).all()) == 1

    published = await service.publish(
        series_id=series_id,
        actor_user_id=admin_id,
        idempotency_key="series-publish-test-key",
        commit=True,
    )
    with pytest.raises(ConflictError, match="DRAW_COMMITMENT_REQUIRED"):
        await service.open(
            series_id=series_id,
            actor_user_id=admin_id,
            idempotency_key="series-open-without-draw-commit-test-key",
            commit=True,
        )
    await session.rollback()
    await WinnerService(session).commit_seed(
        series_id=series_id,
        actor_user_id=admin_id,
        seed_commitment=sha256(b"series-lifecycle-draw-seed").hexdigest(),
        idempotency_key="series-draw-commit-test-key",
        commit=True,
    )
    opened = await service.open(
        series_id=series_id,
        actor_user_id=admin_id,
        idempotency_key="series-open-test-key",
        commit=True,
    )
    assert published.response_payload["status"] == TicketSeriesStatus.PUBLISHED.value
    assert opened.series.status == TicketSeriesStatus.OPEN


@pytest.mark.asyncio
async def test_order_reserves_limited_series_then_cancellation_releases_it(session: AsyncSession) -> None:
    admin = await _admin(session)
    buyer = await _buyer(session)
    series = await _open_series(session, admin, name="Reserve me", ticket_limit=3)
    service = OrderService(session)
    created = await service.create(
        buyer_user_id=buyer.id,
        selection=OrderSelection(TicketProductType.SERIES, series.id, 2),
        idempotency_key="order-create-test-key",
        commit=True,
    )
    replay = await service.create(
        buyer_user_id=buyer.id,
        selection=OrderSelection(TicketProductType.SERIES, series.id, 2),
        idempotency_key="order-create-test-key",
        commit=True,
    )
    current_series = await session.get(TicketSeries, series.id)
    created_order_id = created.order.id
    buyer_id = buyer.id
    assert created.order.status == OrderStatus.PENDING_PAYMENT
    assert replay.replayed is True
    assert current_series is not None and current_series.reserved_count == 2 and current_series.sold_count == 0

    with pytest.raises(ConflictError, match="INSUFFICIENT_TICKET_INVENTORY"):
        await service.create(
            buyer_user_id=buyer.id,
            selection=OrderSelection(TicketProductType.SERIES, series.id, 2),
            idempotency_key="order-capacity-test-key",
            commit=False,
        )
    await session.rollback()

    cancelled = await service.cancel(
        order_id=created_order_id,
        actor_user_id=buyer_id,
        idempotency_key="order-cancel-test-key",
        commit=True,
    )
    current_series = await session.get(TicketSeries, series.id)
    assert cancelled.order.status == OrderStatus.CANCELLED
    assert current_series is not None and current_series.reserved_count == 0


@pytest.mark.asyncio
async def test_catalog_update_replays_original_response_after_lifecycle_moves_on(session: AsyncSession) -> None:
    admin = await _admin(session)
    now = datetime.now(timezone.utc)
    series_service = TicketSeriesService(session)
    created = await series_service.create(
        actor_user_id=admin.id,
        name="Replay update",
        description="Before update",
        price_paise=3_000,
        ticket_limit=4,
        sales_start_at=now - timedelta(minutes=1),
        sales_end_at=now + timedelta(hours=1),
        draw_at=now + timedelta(days=1),
        prizes=[PrizeDraft(rank=1, title="Top", prize_paise=5_000)],
        idempotency_key="replay-update-create-key",
        commit=True,
    )
    updated = await series_service.update(
        series_id=created.series.id,
        actor_user_id=admin.id,
        name="Replay update revised",
        idempotency_key="replay-update-key-0001",
        commit=True,
    )
    await series_service.publish(
        series_id=created.series.id,
        actor_user_id=admin.id,
        idempotency_key="replay-update-publish",
        commit=True,
    )
    await WinnerService(session).commit_seed(
        series_id=created.series.id,
        actor_user_id=admin.id,
        seed_commitment=sha256(b"replay-update-draw-seed").hexdigest(),
        idempotency_key="replay-update-draw-commit-001",
        commit=True,
    )
    await series_service.open(
        series_id=created.series.id,
        actor_user_id=admin.id,
        idempotency_key="replay-update-open-001",
        commit=True,
    )
    replay = await series_service.update(
        series_id=created.series.id,
        actor_user_id=admin.id,
        name="Replay update revised",
        idempotency_key="replay-update-key-0001",
        commit=True,
    )
    assert replay.replayed is True
    assert replay.response_payload == updated.response_payload
    assert replay.response_payload["status"] == TicketSeriesStatus.DRAFT.value


@pytest.mark.asyncio
async def test_package_update_replays_after_reservation_locks_edits(session: AsyncSession) -> None:
    admin = await _admin(session)
    buyer = await _buyer(session)
    series = await _open_series(session, admin, name="Package replay series", ticket_limit=4)
    package_service = TicketPackageService(session)
    package = await package_service.create(
        actor_user_id=admin.id,
        name="Replay package",
        description="Before update",
        price_paise=3_000,
        inventory_limit=None,
        items=[PackageItemDraft(series.id, 1)],
        idempotency_key="package-replay-create-key",
        commit=True,
    )
    updated = await package_service.update(
        package_id=package.package.id,
        actor_user_id=admin.id,
        name="Replay package revised",
        idempotency_key="package-replay-update-key",
        commit=True,
    )
    await OrderService(session).create(
        buyer_user_id=buyer.id,
        selection=OrderSelection(TicketProductType.PACKAGE, package.package.id, 1),
        idempotency_key="package-replay-order-key",
        commit=True,
    )
    replay = await package_service.update(
        package_id=package.package.id,
        actor_user_id=admin.id,
        name="Replay package revised",
        idempotency_key="package-replay-update-key",
        commit=True,
    )
    assert replay.replayed is True
    assert replay.response_payload == updated.response_payload


@pytest.mark.asyncio
async def test_paid_order_allocates_unique_tickets_exactly_once(session: AsyncSession) -> None:
    admin = await _admin(session)
    buyer = await _buyer(session)
    series = await _open_series(session, admin, name="Allocate me", ticket_limit=5)
    order = await OrderService(session).create(
        buyer_user_id=buyer.id,
        selection=OrderSelection(TicketProductType.SERIES, series.id, 2),
        idempotency_key="allocation-order-create",
        commit=True,
    )

    # This models a trusted Phase 4 settlement transaction.  There is no Phase
    # 3 endpoint or service that can perform this state change from a claim.
    settlement_id = uuid4()
    persisted_order = await session.get(Order, order.order.id)
    assert persisted_order is not None
    persisted_order.status = OrderStatus.PAID
    persisted_order.settlement_reference_id = settlement_id
    persisted_order.settled_at = datetime.now(timezone.utc)
    await session.commit()

    allocator = TicketAllocationService(session)
    allocated = await allocator.allocate_paid_order(
        order_id=order.order.id,
        settlement_reference_id=settlement_id,
        idempotency_key="settled-ticket-allocation",
        actor_user_id=None,
        commit=True,
    )
    replay = await allocator.allocate_paid_order(
        order_id=order.order.id,
        settlement_reference_id=settlement_id,
        idempotency_key="settled-ticket-allocation",
        actor_user_id=None,
        commit=True,
    )
    rows = (await session.scalars(select(Ticket).where(Ticket.series_id == series.id))).all()
    current_series = await session.get(TicketSeries, series.id)
    assert allocated.order.status == OrderStatus.FULFILLED
    assert sorted(ticket.serial_number for ticket in rows) == [1, 2]
    assert current_series is not None and current_series.sold_count == 2 and current_series.reserved_count == 0
    assert replay.replayed is True
    assert [ticket.id for ticket in replay.tickets] == [ticket.id for ticket in allocated.tickets]
    assert replay.response_payload == allocated.response_payload


@pytest.mark.asyncio
async def test_expired_unpaid_order_releases_inventory_without_touching_settlement(session: AsyncSession) -> None:
    admin = await _admin(session)
    buyer = await _buyer(session)
    series = await _open_series(session, admin, name="Expiry", ticket_limit=2)
    service = OrderService(session)
    created = await service.create(
        buyer_user_id=buyer.id,
        selection=OrderSelection(TicketProductType.SERIES, series.id, 1),
        idempotency_key="expiry-order-create-key",
        commit=True,
    )
    order_id = created.order.id
    persisted_order = await session.get(Order, order_id)
    assert persisted_order is not None
    persisted_order.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await session.commit()

    expired = await service.expire_pending(
        order_id=order_id,
        idempotency_key="expiry-order-release-key",
        commit=True,
    )
    current_series = await session.get(TicketSeries, series.id)
    assert expired.order.status == OrderStatus.CANCELLED
    assert expired.order.settlement_reference_id is None
    assert current_series is not None and current_series.reserved_count == 0 and current_series.sold_count == 0


@pytest.mark.asyncio
async def test_package_reserves_and_allocates_each_included_series(session: AsyncSession) -> None:
    admin = await _admin(session)
    buyer = await _buyer(session)
    series_a = await _open_series(session, admin, name="Package A", ticket_limit=8)
    series_b = await _open_series(session, admin, name="Package B", ticket_limit=8)
    package = await TicketPackageService(session).create(
        actor_user_id=admin.id,
        name="Mixed package",
        description="One A and two B tickets",
        price_paise=7_000,
        inventory_limit=3,
        items=[PackageItemDraft(series_a.id, 1), PackageItemDraft(series_b.id, 2)],
        idempotency_key="package-create-test-key",
        commit=True,
    )
    order = await OrderService(session).create(
        buyer_user_id=buyer.id,
        selection=OrderSelection(TicketProductType.PACKAGE, package.package.id, 2),
        idempotency_key="package-order-create-key",
        commit=True,
    )
    a = await session.get(TicketSeries, series_a.id)
    b = await session.get(TicketSeries, series_b.id)
    current_package = await session.get(TicketPackage, package.package.id)
    assert a is not None and a.reserved_count == 2
    assert b is not None and b.reserved_count == 4
    assert current_package is not None and current_package.reserved_count == 2

    settlement_id = uuid4()
    persisted_order = await session.get(Order, order.order.id)
    assert persisted_order is not None
    persisted_order.status = OrderStatus.PAID
    persisted_order.settlement_reference_id = settlement_id
    persisted_order.settled_at = datetime.now(timezone.utc)
    await session.commit()
    allocation = await TicketAllocationService(session).allocate_paid_order(
        order_id=order.order.id,
        settlement_reference_id=settlement_id,
        idempotency_key="package-ticket-allocation",
        actor_user_id=None,
        commit=True,
    )
    a = await session.get(TicketSeries, series_a.id)
    b = await session.get(TicketSeries, series_b.id)
    current_package = await session.get(TicketPackage, package.package.id)
    assert len(allocation.tickets) == 6
    assert a is not None and (a.sold_count, a.reserved_count) == (2, 0)
    assert b is not None and (b.sold_count, b.reserved_count) == (4, 0)
    assert current_package is not None and (current_package.sold_count, current_package.reserved_count) == (2, 0)
