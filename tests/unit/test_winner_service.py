"""Phase 6 draw, prize, and publication service coverage."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.idempotency import fingerprint
from app.core.permissions import RoleName
from app.exceptions import ConflictError, ValidationError
from app.models.audit_log import AuditLog
from app.models.ledger import JournalPosting
from app.models.order import DeliveryStatus, Order, OrderItem, OrderStatus, TicketProductType
from app.models.ticket import Ticket, TicketStatus
from app.models.ticket_series import TicketSeries, TicketSeriesPrize, TicketSeriesStatus
from app.models.user import Role, User
from app.models.wallet import Wallet
from app.models.winner import Draw, DrawNotification, DrawStatus, PrizeAward, Winner
from app.services.result_service import ResultService
from app.services.ticket_series_service import TicketSeriesService
from app.services.winner_service import DRAW_ALGORITHM_VERSION, WinnerService


async def _draw_fixture(session: AsyncSession) -> tuple[User, TicketSeries, list[User], str]:
    """Create a published, uncommitted series before its sales can open."""

    now = datetime.now(timezone.utc)
    admin_role = Role(id=uuid4(), name=RoleName.ADMIN.value)
    admin = User(id=uuid4(), email=f"draw-admin-{uuid4()}@example.test", password_hash="test", roles=[admin_role])
    buyers = [
        User(id=uuid4(), email=f"draw-buyer-{uuid4()}@example.test", password_hash="test")
        for _ in range(3)
    ]
    series = TicketSeries(
        id=uuid4(),
        name="Auditable draw",
        description="Phase 6 service test",
        price_paise=3_000,
        ticket_limit=10,
        sold_count=0,
        reserved_count=0,
        currency="INR",
        sales_start_at=now - timedelta(hours=1),
        sales_end_at=now + timedelta(hours=1),
        draw_at=now + timedelta(hours=2),
        status=TicketSeriesStatus.PUBLISHED,
        created_by_user_id=admin.id,
    )
    prizes = [
        TicketSeriesPrize(id=uuid4(), series_id=series.id, rank=1, title="First", prize_paise=10_000),
        TicketSeriesPrize(id=uuid4(), series_id=series.id, rank=2, title="Second", prize_paise=5_000),
    ]
    session.add_all([admin_role, admin, *buyers, series, *prizes])
    await session.commit()
    return admin, series, buyers, "phase6-secret-seed-value-0001"


async def _open_committed_series(session: AsyncSession, *, series: TicketSeries, admin: User) -> TicketSeries:
    """Use the catalog transition that refuses to open without a commitment."""

    return (
        await TicketSeriesService(session).open(
            series_id=series.id,
            actor_user_id=admin.id,
            idempotency_key=f"phase6-open-{uuid4()}",
            commit=True,
        )
    ).series


async def _record_sold_and_void_tickets(
    session: AsyncSession, *, series: TicketSeries, buyers: list[User]
) -> list[Ticket]:
    """Insert settlement-created tickets after the commitment is durable."""

    now = datetime.now(timezone.utc)
    tickets: list[Ticket] = []
    for index, buyer in enumerate(buyers, start=1):
        order = Order(
            id=uuid4(), buyer_user_id=buyer.id, status=OrderStatus.FULFILLED,
            delivery_status=DeliveryStatus.DELIVERED, total_paise=3_000, currency="INR",
            expires_at=now + timedelta(hours=1), settlement_reference_id=uuid4(), settled_at=now,
        )
        item = OrderItem(
            id=uuid4(), order_id=order.id, product_type=TicketProductType.SERIES,
            product_id=series.id, product_name_snapshot=series.name, unit_price_paise=3_000,
            quantity=1, line_total_paise=3_000, currency="INR",
        )
        ticket = Ticket(
            id=uuid4(), series_id=series.id, serial_number=index, order_item_id=item.id,
            owner_user_id=buyer.id, status=TicketStatus.ALLOCATED, is_winner=False, prize_paise=0,
        )
        session.add_all([order, item, ticket])
        tickets.append(ticket)

    void_order = Order(
        id=uuid4(), buyer_user_id=buyers[0].id, status=OrderStatus.FULFILLED,
        delivery_status=DeliveryStatus.DELIVERED, total_paise=3_000, currency="INR",
        expires_at=now + timedelta(hours=1), settlement_reference_id=uuid4(), settled_at=now,
    )
    void_item = OrderItem(
        id=uuid4(), order_id=void_order.id, product_type=TicketProductType.SERIES,
        product_id=series.id, product_name_snapshot=series.name, unit_price_paise=3_000,
        quantity=1, line_total_paise=3_000, currency="INR",
    )
    session.add_all(
        [
            void_order,
            void_item,
            Ticket(
                id=uuid4(), series_id=series.id, serial_number=4, order_item_id=void_item.id,
                owner_user_id=buyers[0].id, status=TicketStatus.VOID, is_winner=False, prize_paise=0,
            ),
        ]
    )
    series.sold_count = 4
    await session.commit()
    return tickets


async def _move_draw_window_into_the_past(session: AsyncSession, series: TicketSeries) -> None:
    now = datetime.now(timezone.utc)
    series.sales_end_at = now - timedelta(minutes=2)
    series.draw_at = now - timedelta(minutes=1)
    await session.commit()


@pytest.mark.asyncio
async def test_commit_reveal_draw_posts_prizes_and_publishes_a_reproducible_result(session: AsyncSession) -> None:
    admin, series, buyers, seed = await _draw_fixture(session)
    commitment = sha256(seed.encode("utf-8")).hexdigest()
    winners = WinnerService(session)

    committed = await winners.commit_seed(
        series_id=series.id,
        actor_user_id=admin.id,
        seed_commitment=commitment,
        idempotency_key="phase6-commit-0001",
        commit=True,
    )
    assert committed.draw.status == DrawStatus.COMMITTED
    assert committed.response_payload["seed_revealed"] is False
    assert "seed_reveal" not in committed.response_payload

    series = await _open_committed_series(session, series=series, admin=admin)
    eligible_tickets = await _record_sold_and_void_tickets(session, series=series, buyers=buyers)
    await _move_draw_window_into_the_past(session, series)
    closed = await winners.close_sales(
        series_id=series.id,
        actor_user_id=admin.id,
        idempotency_key="phase6-close-00001",
        commit=True,
    )
    assert closed.draw.status == DrawStatus.SALES_CLOSED
    assert closed.series.status == TicketSeriesStatus.CLOSED
    assert closed.draw.eligible_ticket_count == len(eligible_tickets)

    drawn = await winners.run_draw(
        series_id=series.id,
        actor_user_id=admin.id,
        seed_reveal=seed,
        idempotency_key="phase6-run-0000001",
        commit=True,
    )
    assert drawn.draw.status == DrawStatus.DRAWN
    assert drawn.series.status == TicketSeriesStatus.DRAWN
    selected_serials = {winner["ticket_serial_number"] for winner in drawn.response_payload["winners"]}
    assert selected_serials <= {ticket.serial_number for ticket in eligible_tickets}
    assert len(selected_serials) == 2

    awarded = await winners.post_prizes(
        series_id=series.id,
        actor_user_id=admin.id,
        idempotency_key="phase6-awards-0001",
        commit=True,
    )
    replayed = await winners.post_prizes(
        series_id=series.id,
        actor_user_id=admin.id,
        idempotency_key="phase6-awards-0001",
        commit=True,
    )
    assert awarded.draw.status == DrawStatus.PRIZES_POSTED
    assert replayed.replayed is True
    prize_awards = list(await session.scalars(select(PrizeAward).where(PrizeAward.draw_id == awarded.draw.id)))
    assert len(prize_awards) == 2
    wallet_total = await session.scalar(select(func.coalesce(func.sum(Wallet.available_paise), 0)))
    assert wallet_total == 15_000
    posting_count = await session.scalar(select(func.count()).select_from(JournalPosting))
    assert posting_count == 4

    published = await winners.publish_results(
        series_id=series.id,
        actor_user_id=admin.id,
        idempotency_key="phase6-publish-001",
        commit=True,
    )
    assert published.draw.status == DrawStatus.RESULT_PUBLISHED
    assert published.series.status == TicketSeriesStatus.RESULT_PUBLISHED
    assert await session.scalar(select(func.count()).select_from(DrawNotification)) == 2

    public = await ResultService(session).get_published(series_id=series.id)
    assert public["seed_reveal"] == seed
    assert "eligible_ticket_serial_numbers" not in public
    first_candidate_page = await ResultService(session).get_published_candidates(
        series_id=series.id,
        limit=1,
    )
    assert first_candidate_page["ticket_serial_numbers"] == [1]
    assert first_candidate_page["next_after_serial_number"] == 1
    second_candidate_page = await ResultService(session).get_published_candidates(
        series_id=series.id,
        after_serial_number=int(first_candidate_page["next_after_serial_number"]),
        limit=2,
    )
    assert second_candidate_page["ticket_serial_numbers"] == [2, 3]
    assert second_candidate_page["next_after_serial_number"] is None
    eligible_serial_numbers = [
        *first_candidate_page["ticket_serial_numbers"],
        *second_candidate_page["ticket_serial_numbers"],
    ]
    assert {winner["ticket_serial_number"] for winner in public["winners"]} == selected_serials
    assert all("owner_user_id" not in winner for winner in public["winners"])
    candidate_digest = WinnerService._candidate_digest(
        series_id=series.id,
        algorithm_version=DRAW_ALGORITHM_VERSION,
        entries=[{"ticket_serial_number": serial} for serial in eligible_serial_numbers],
    )
    assert candidate_digest == public["eligible_tickets_digest"]
    remaining_serials = list(eligible_serial_numbers)
    for winner in public["winners"]:
        index, counter = WinnerService._select_index(
            seed_reveal=seed,
            eligible_tickets_digest=candidate_digest,
            rank=winner["rank"],
            population_size=len(remaining_serials),
        )
        assert remaining_serials.pop(index) == winner["ticket_serial_number"]
        assert counter == winner["selection_counter"]
        assert winner["selection_digest"] == fingerprint(
            {
                "algorithm_version": DRAW_ALGORITHM_VERSION,
                "eligible_tickets_digest": candidate_digest,
                "rank": winner["rank"],
                "selection_counter": counter,
                "ticket_serial_number": winner["ticket_serial_number"],
            }
        )
    assert public["result_digest"] == fingerprint(
        {
            "algorithm_version": public["algorithm_version"],
            "seed_commitment": public["seed_commitment"],
            "seed_reveal": public["seed_reveal"],
            "eligible_ticket_count": public["eligible_ticket_count"],
            "eligible_tickets_digest": public["eligible_tickets_digest"],
            "prize_snapshot": public["prize_snapshot"],
            "winners": public["winners"],
        }
    )
    assert await session.scalar(select(func.count()).select_from(AuditLog)) >= 10


@pytest.mark.asyncio
async def test_draw_requires_commitment_and_matching_reveal(session: AsyncSession) -> None:
    admin, series, buyers, seed = await _draw_fixture(session)
    admin_id = admin.id
    series_id = series.id
    buyer_ids = [buyer.id for buyer in buyers]
    winners = WinnerService(session)
    await _move_draw_window_into_the_past(session, series)
    with pytest.raises(ConflictError, match="DRAW_COMMITMENT_REQUIRED"):
        await winners.close_sales(
            series_id=series_id,
            actor_user_id=admin_id,
            idempotency_key="phase6-close-no-commit",
            commit=True,
        )
    await session.rollback()

    # Commit must happen before sales close, so restore a valid window for the
    # commitment, then advance it again for deterministic test closure.
    now = datetime.now(timezone.utc)
    series = await session.get(TicketSeries, series_id)
    assert series is not None
    buyers = [await session.get(User, buyer_id) for buyer_id in buyer_ids]
    assert all(buyer is not None for buyer in buyers)
    buyers = [buyer for buyer in buyers if buyer is not None]
    series.sales_end_at = now + timedelta(hours=1)
    series.draw_at = now + timedelta(hours=2)
    await session.commit()
    await winners.commit_seed(
        series_id=series_id,
        actor_user_id=admin_id,
        seed_commitment=sha256(seed.encode("utf-8")).hexdigest(),
        idempotency_key="phase6-commit-0002",
        commit=True,
    )
    series = await session.get(TicketSeries, series_id)
    assert series is not None
    series = await _open_committed_series(session, series=series, admin=admin)
    await _record_sold_and_void_tickets(session, series=series, buyers=buyers)
    await _move_draw_window_into_the_past(session, series)
    await winners.close_sales(
        series_id=series_id,
        actor_user_id=admin_id,
        idempotency_key="phase6-close-00002",
        commit=True,
    )
    with pytest.raises(ValidationError, match="DRAW_SEED_COMMITMENT_MISMATCH"):
        await winners.run_draw(
            series_id=series_id,
            actor_user_id=admin_id,
            seed_reveal="different-phase6-secret-seed-value",
            idempotency_key="phase6-run-invalid-1",
            commit=True,
        )
    await session.rollback()
    draw = await session.scalar(select(Draw).where(Draw.series_id == series_id))
    assert draw is not None and draw.status == DrawStatus.SALES_CLOSED
    assert await session.scalar(select(func.count()).select_from(Winner)) == 0


@pytest.mark.asyncio
async def test_commitment_is_rejected_once_any_ticket_has_been_sold(session: AsyncSession) -> None:
    admin, series, buyers, seed = await _draw_fixture(session)
    series.status = TicketSeriesStatus.OPEN
    await session.commit()
    await _record_sold_and_void_tickets(session, series=series, buyers=buyers)

    with pytest.raises(ConflictError, match="DRAW_COMMIT_SERIES_NOT_PUBLISHED"):
        await WinnerService(session).commit_seed(
            series_id=series.id,
            actor_user_id=admin.id,
            seed_commitment=sha256(seed.encode("utf-8")).hexdigest(),
            idempotency_key="phase6-commit-after-sale",
            commit=True,
        )


@pytest.mark.asyncio
async def test_committed_published_series_cannot_be_edited(session: AsyncSession) -> None:
    admin, series, _buyers, seed = await _draw_fixture(session)
    await WinnerService(session).commit_seed(
        series_id=series.id,
        actor_user_id=admin.id,
        seed_commitment=sha256(seed.encode("utf-8")).hexdigest(),
        idempotency_key="phase6-commit-edit-lock-001",
        commit=True,
    )

    with pytest.raises(ConflictError, match="SERIES_EDIT_LOCKED"):
        await TicketSeriesService(session).update(
            series_id=series.id,
            actor_user_id=admin.id,
            name="Tampered after commitment",
            idempotency_key="phase6-edit-after-commit-001",
            commit=True,
        )


def test_seed_reveal_rejects_control_characters() -> None:
    with pytest.raises(ValidationError, match="INVALID_DRAW_REVEAL"):
        WinnerService._validate_reveal(chr(0) * 16)
