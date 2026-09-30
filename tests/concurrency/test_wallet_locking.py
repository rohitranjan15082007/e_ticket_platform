"""PostgreSQL integration coverage for row-level wallet locking.

Set ``TICKET_TEST_DATABASE_URL`` to a dedicated database whose name ends in
``_test`` (for example, ``ticket_platform_test``). The fixture intentionally
runs Alembic down to ``base`` before and after the test, so it will never run
against a database that does not clearly identify itself as a test database.
"""

import asyncio
import os
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.core.permissions import RoleName
from app.core.security import hash_password
from app.exceptions import ConflictError, InsufficientFundsError
from app.models.audit_log import AuditLog
from app.models import Base
from app.models.ledger import JournalGroup, JournalPosting, LedgerAccount, PostingDirection
from app.models.order import DeliveryStatus, Order, OrderStatus, TicketProductType, TicketReservation
from app.models.p2p_match import (
    P2PMatch,
    P2PMatchStatus,
    P2PPaymentSubmission,
    P2PSettlement,
    P2PVerifiedPaymentReference,
    PaymentSubmissionVerificationStatus,
)
from app.models.ticket import Ticket
from app.models.ticket_series import TicketSeries, TicketSeriesStatus
from app.models.user import IdempotencyRecord, Role, User
from app.models.wallet import Wallet, WalletHold, WalletHoldStatus
from app.models.withdrawal import (
    PaymentDestination,
    PaymentDestinationStatus,
    WithdrawalRequest,
    WithdrawalStatus,
)
from app.models.winner import Draw, DrawStatus
from app.services.ledger_service import LedgerService
from app.services.order_service import OrderSelection, OrderService
from app.services.p2p_service import P2PService
from app.services.ticket_allocation_service import TicketAllocationService
from app.services.ticket_series_service import PrizeDraft, TicketSeriesService
from app.services.wallet_service import WalletMutationResult, WalletService
from app.services.withdrawal_service import WithdrawalService
from app.services.winner_service import WinnerService


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_REVISION = "head"


def _safe_postgres_test_url() -> tuple[str | None, str]:
    """Allow destructive migration setup only for an explicit test database."""

    raw_url = os.getenv("TICKET_TEST_DATABASE_URL")
    if not raw_url:
        return None, "requires TICKET_TEST_DATABASE_URL for an isolated PostgreSQL database"
    try:
        parsed = make_url(raw_url)
    except ArgumentError:
        return None, "TICKET_TEST_DATABASE_URL must be a valid SQLAlchemy URL"
    if parsed.get_backend_name() != "postgresql" or parsed.get_driver_name() != "asyncpg":
        return None, "TICKET_TEST_DATABASE_URL must use the postgresql+asyncpg driver"
    if not parsed.database or not parsed.database.lower().endswith("_test"):
        return None, "TICKET_TEST_DATABASE_URL database name must end with _test"
    return raw_url, ""


POSTGRES_TEST_URL, SKIP_REASON = _safe_postgres_test_url()
pytestmark = pytest.mark.skipif(POSTGRES_TEST_URL is None, reason=SKIP_REASON)


def _alembic_config(database_url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


async def _clear_migrated_test_rows(database_url: str) -> None:
    """Remove test-created rows before downgrading data-dependent constraints."""

    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            existing = set(
                (await connection.scalars(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )).all()
            )
            model_tables = sorted(existing.intersection(Base.metadata.tables))
            if model_tables:
                quote = connection.dialect.identifier_preparer.quote
                table_list = ", ".join(f"public.{quote(name)}" for name in model_tables)
                await connection.execute(text(f"TRUNCATE TABLE {table_list} CASCADE"))
    finally:
        await engine.dispose()


@pytest.fixture
def migrated_postgres_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Reset only the explicitly named test DB, then apply real migrations."""

    assert POSTGRES_TEST_URL is not None
    monkeypatch.setenv("TICKET_DATABASE_URL", POSTGRES_TEST_URL)
    get_settings.cache_clear()
    config = _alembic_config(POSTGRES_TEST_URL)
    try:
        asyncio.run(_clear_migrated_test_rows(POSTGRES_TEST_URL))
        command.downgrade(config, "base")
        command.upgrade(config, SCHEMA_REVISION)
        yield POSTGRES_TEST_URL
    finally:
        get_settings.cache_clear()
        asyncio.run(_clear_migrated_test_rows(POSTGRES_TEST_URL))
        command.downgrade(config, "base")
        get_settings.cache_clear()


async def _create_matched_p2p_flow(
    session, *, label: str, amount_paise: int = 50_000
) -> tuple[UUID, UUID, UUID]:
    """Create an isolated buyer/receiver match using the real service flow."""

    receiver = User(email=f"p2p-reference-{label}-receiver@example.com", password_hash="test-only-hash")
    buyer = User(email=f"p2p-reference-{label}-buyer@example.com", password_hash="test-only-hash")
    session.add_all((receiver, buyer))
    await session.commit()

    wallet = (
        await WalletService(session).provision_wallet(
            user_id=receiver.id,
            actor_user_id=receiver.id,
            currency="INR",
            idempotency_key=f"p2p-reference-{label}-provision-001",
            commit=True,
        )
    ).wallet
    await WalletService(session).credit_available(
        wallet_id=wallet.id,
        actor_user_id=None,
        amount_paise=100_000,
        idempotency_key=f"p2p-reference-{label}-credit-001",
        source_type="p2p-reference-race-funding",
        source_id=uuid4(),
        reason="test-only funding",
        commit=True,
    )
    destination = PaymentDestination(
        user_id=receiver.id,
        provider_namespace="upi",
        display_label="R***@upi",
        destination_data={"upi_id": f"{label}-receiver@upi"},
        status=PaymentDestinationStatus.VERIFIED,
        verification_method="test-controlled-verification",
        verification_evidence_reference=f"p2p-reference-{label}-evidence",
        verified_at=datetime.now(timezone.utc),
    )
    session.add(destination)
    await session.commit()
    await WithdrawalService(session).create(
        actor_user_id=receiver.id,
        amount_paise=amount_paise,
        payment_destination_id=destination.id,
        idempotency_key=f"p2p-reference-{label}-withdrawal-001",
        commit=True,
    )
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
    result = await P2PService(session).queue_or_match_order(
        order_id=order.id,
        buyer_user_id=buyer.id,
        idempotency_key=f"p2p-reference-{label}-match-001",
        commit=True,
    )
    assert result.match is not None
    return receiver.id, buyer.id, result.match.id


@pytest.mark.asyncio
async def test_parallel_holds_cannot_overdraw_a_migrated_locked_wallet(
    migrated_postgres_url: str,
) -> None:
    """One of two simultaneous 7,500-paise holds against 10,000 must lose safely."""

    engine = create_async_engine(migrated_postgres_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as setup_session:
            user = User(
                email="concurrent-wallet@example.com",
                password_hash=hash_password("correct-horse-battery-staple"),
            )
            setup_session.add(user)
            await setup_session.commit()
            wallet = (
                await WalletService(setup_session).provision_wallet(
                    user_id=user.id,
                    actor_user_id=user.id,
                    currency="INR",
                    idempotency_key="concurrency-provision-001",
                    commit=True,
                )
            ).wallet
            await WalletService(setup_session).credit_available(
                wallet_id=wallet.id,
                actor_user_id=None,
                amount_paise=10_000,
                idempotency_key="concurrency-credit-001",
                source_type="concurrency-test-funding",
                source_id=uuid4(),
                reason="test-only funding",
                commit=True,
            )

        start = asyncio.Barrier(2)

        async def attempt_hold(index: int) -> WalletMutationResult | InsufficientFundsError:
            async with sessions() as session:
                await start.wait()
                try:
                    return await WalletService(session).hold_available(
                        wallet_id=wallet.id,
                        actor_user_id=user.id,
                        amount_paise=7_500,
                        idempotency_key=f"concurrency-hold-{index}",
                        reference_type="concurrency-test",
                        reference_id=uuid4(),
                        reason="race test",
                        commit=True,
                    )
                except InsufficientFundsError as error:
                    await session.rollback()
                    return error

        outcomes = await asyncio.wait_for(
            asyncio.gather(attempt_hold(1), attempt_hold(2)), timeout=15
        )
        successful = [outcome for outcome in outcomes if isinstance(outcome, WalletMutationResult)]
        insufficient = [outcome for outcome in outcomes if isinstance(outcome, InsufficientFundsError)]

        assert len(successful) == 1
        assert len(insufficient) == 1
        assert len(outcomes) == len(successful) + len(insufficient)

        async with sessions() as verification_session:
            stored = await verification_session.get(Wallet, wallet.id)
            holds = list(
                await verification_session.scalars(
                    select(WalletHold).where(WalletHold.wallet_id == wallet.id)
                )
            )
            groups = list(
                await verification_session.scalars(
                    select(JournalGroup).where(JournalGroup.event_type == "WALLET_FUNDS_HELD")
                )
            )
            hold_audits = await verification_session.scalar(
                select(func.count()).select_from(AuditLog).where(AuditLog.action == "WALLET_FUNDS_HELD")
            )
            hold_creation_audits = await verification_session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "WALLET_HOLD_CREATED")
            )

            assert stored is not None
            assert stored.available_paise == 2_500
            assert stored.locked_paise == 7_500
            assert stored.available_paise + stored.locked_paise == 10_000
            assert len(holds) == 1
            assert holds[0].amount_paise == 7_500
            assert len(groups) == 1
            assert successful[0].journal_group is not None
            assert groups[0].id == successful[0].journal_group.id
            assert hold_audits == 1
            assert hold_creation_audits == 1
            await LedgerService(verification_session).assert_group_balanced(groups[0].id)

        replay_barrier = asyncio.Barrier(2)
        replay_source_id = uuid4()

        async def repeat_same_credit() -> WalletMutationResult:
            async with sessions() as session:
                await replay_barrier.wait()
                return await WalletService(session).credit_available(
                    wallet_id=wallet.id,
                    actor_user_id=None,
                    amount_paise=1_000,
                    idempotency_key="concurrency-credit-idempotency-001",
                    source_type="concurrency-idempotency-source",
                    source_id=replay_source_id,
                    reason="same key race test",
                    commit=True,
                )

        replay_results = await asyncio.wait_for(
            asyncio.gather(repeat_same_credit(), repeat_same_credit()), timeout=15
        )
        assert sorted(result.replayed for result in replay_results) == [False, True]
        assert replay_results[0].journal_group is not None
        assert replay_results[1].journal_group is not None
        assert replay_results[0].journal_group.id == replay_results[1].journal_group.id

        async with sessions() as verification_session:
            stored = await verification_session.get(Wallet, wallet.id)
            credit_groups = await verification_session.scalar(
                select(func.count())
                .select_from(JournalGroup)
                .where(JournalGroup.event_type == "WALLET_CREDITED")
            )
            credit_audits = await verification_session.scalar(
                select(func.count()).select_from(AuditLog).where(AuditLog.action == "WALLET_CREDITED")
            )
            credit_keys = await verification_session.scalar(
                select(func.count())
                .select_from(IdempotencyRecord)
                .where(
                    IdempotencyRecord.actor_scope == "system:wallet.credit",
                    IdempotencyRecord.idempotency_key == "concurrency-credit-idempotency-001",
                )
            )

            assert stored is not None
            assert stored.available_paise == 3_500
            assert stored.locked_paise == 7_500
            assert credit_groups == 2
            assert credit_audits == 2
            assert credit_keys == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgresql_financial_history_is_append_only_and_rejects_unbalanced_groups(
    migrated_postgres_url: str,
) -> None:
    """Database guards must reject direct SQL writes that bypass service validation."""

    engine = create_async_engine(migrated_postgres_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            user = User(
                email="financial-guard@example.com",
                password_hash=hash_password("correct-horse-battery-staple"),
            )
            session.add(user)
            await session.commit()
            user_id = user.id
            wallet = (
                await WalletService(session).provision_wallet(
                    user_id=user_id,
                    actor_user_id=user_id,
                    currency="INR",
                    idempotency_key="financial-guard-provision-001",
                    commit=True,
                )
            ).wallet
            credit = await WalletService(session).credit_available(
                wallet_id=wallet.id,
                actor_user_id=None,
                amount_paise=1_000,
                idempotency_key="financial-guard-credit-001",
                source_type="financial-guard-source",
                source_id=uuid4(),
                reason="financial guard test funding",
                commit=True,
            )
            assert credit.journal_group is not None

            with pytest.raises(DBAPIError):
                await session.execute(
                    update(JournalGroup)
                    .where(JournalGroup.id == credit.journal_group.id)
                    .values(reason="tamper")
                )
                await session.commit()
            await session.rollback()

            audit = await session.scalar(select(AuditLog).limit(1))
            assert audit is not None
            with pytest.raises(DBAPIError):
                await session.execute(delete(AuditLog).where(AuditLog.id == audit.id))
                await session.commit()
            await session.rollback()

            account = await session.scalar(select(LedgerAccount).limit(1))
            assert account is not None
            bad_group_id = uuid4()
            bad_record = IdempotencyRecord(
                id=uuid4(),
                actor_scope="system:financial-guard",
                idempotency_key="financial-guard-unbalanced-001",
                request_fingerprint="0" * 64,
                resource_type="journal_group",
                resource_id=bad_group_id,
                status_code=200,
                response_payload={"test": "unbalanced"},
            )
            session.add(bad_record)
            await session.flush()
            session.add_all(
                [
                    JournalGroup(
                        id=bad_group_id,
                        event_type="DIRECT_TAMPER",
                        currency="INR",
                        reference_type="financial-guard",
                        reference_id=uuid4(),
                        idempotency_record_id=bad_record.id,
                        actor_user_id=user_id,
                        reason="must fail at deferred constraint",
                    ),
                    JournalPosting(
                        id=uuid4(),
                        journal_group_id=bad_group_id,
                        account_id=account.id,
                        direction=PostingDirection.DEBIT,
                        amount_paise=100,
                        currency="INR",
                    ),
                ]
            )
            with pytest.raises(DBAPIError):
                await session.commit()
            await session.rollback()

            assert await session.get(JournalGroup, bad_group_id) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgresql_draw_evidence_guards_reject_tampering(
    migrated_postgres_url: str,
) -> None:
    """Phase 6 evidence must remain immutable and draw-scoped in PostgreSQL."""

    engine = create_async_engine(migrated_postgres_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            now = datetime.now(timezone.utc)
            admin_id = uuid4()
            admin = User(
                id=admin_id,
                email="draw-guard-admin@example.com",
                password_hash="test-only-hash",
            )
            series = TicketSeries(
                id=uuid4(),
                name="Draw integrity guard",
                description="PostgreSQL evidence trigger coverage",
                price_paise=3_000,
                ticket_limit=2,
                sold_count=0,
                reserved_count=0,
                currency="INR",
                sales_start_at=now - timedelta(hours=1),
                sales_end_at=now + timedelta(hours=1),
                draw_at=now + timedelta(hours=2),
                status=TicketSeriesStatus.PUBLISHED,
                created_by_user_id=admin_id,
            )
            series_id = series.id
            session.add(admin)
            await session.flush()
            session.add(series)
            await session.commit()

            with pytest.raises(DBAPIError):
                await session.execute(
                    update(TicketSeries)
                    .where(TicketSeries.id == series_id)
                    .values(status=TicketSeriesStatus.OPEN)
                )
                await session.commit()
            await session.rollback()

            draw_id = uuid4()
            draw = Draw(
                id=draw_id,
                series_id=series_id,
                status=DrawStatus.COMMITTED,
                algorithm_version="sha256-rejection-sampling-v1",
                seed_commitment="a" * 64,
                eligible_ticket_count=0,
                committed_by_user_id=admin_id,
                committed_at=now,
            )
            session.add(draw)
            await session.commit()

            constraints = set(
                (
                    await session.scalars(
                        text(
                            "SELECT conname FROM pg_constraint "
                            "WHERE conname IN ("
                            "'fk_winner_draw_entry', "
                            "'fk_prize_award_draw_winner', "
                            "'fk_draw_notification_draw_winner'"
                            ")"
                        )
                    )
                ).all()
            )
            assert constraints == {
                "fk_winner_draw_entry",
                "fk_prize_award_draw_winner",
                "fk_draw_notification_draw_winner",
            }
            triggers = set(
                (
                    await session.scalars(
                        text(
                            "SELECT tgname FROM pg_trigger "
                            "WHERE tgname IN ("
                            "'ticket_series_open_requires_draw_commitment', "
                            "'draws_controlled_transition', "
                            "'draw_entries_append_only', "
                            "'winners_append_only', "
                            "'prize_awards_append_only', "
                            "'draw_notifications_append_only'"
                            ")"
                        )
                    )
                ).all()
            )
            assert triggers == {
                "ticket_series_open_requires_draw_commitment",
                "draws_controlled_transition",
                "draw_entries_append_only",
                "winners_append_only",
                "prize_awards_append_only",
                "draw_notifications_append_only",
            }

            with pytest.raises(DBAPIError):
                await session.execute(
                    update(Draw).where(Draw.id == draw_id).values(seed_commitment="b" * 64)
                )
                await session.commit()
            await session.rollback()

            with pytest.raises(DBAPIError):
                await session.execute(
                    update(Draw).where(Draw.id == draw_id).values(status=DrawStatus.DRAWN)
                )
                await session.commit()
            await session.rollback()
    finally:
        await engine.dispose()


async def _create_open_series(session, *, admin: User, ticket_limit: int) -> TicketSeries:
    """Create an OPEN series using the same public domain lifecycle as production."""

    now = datetime.now(timezone.utc)
    service = TicketSeriesService(session)
    created = await service.create(
        actor_user_id=admin.id,
        name=f"Concurrent inventory {uuid4()}",
        description="PostgreSQL row-lock test",
        price_paise=3_000,
        ticket_limit=ticket_limit,
        sales_start_at=now - timedelta(minutes=1),
        sales_end_at=now + timedelta(hours=1),
        draw_at=now + timedelta(days=1),
        prizes=[PrizeDraft(rank=1, title="Test prize", prize_paise=10_000)],
        idempotency_key=f"concurrent-series-create-{uuid4()}",
        commit=True,
    )
    await service.publish(
        series_id=created.series.id,
        actor_user_id=admin.id,
        idempotency_key=f"concurrent-series-publish-{uuid4()}",
        commit=True,
    )
    await WinnerService(session).commit_seed(
        series_id=created.series.id,
        actor_user_id=admin.id,
        seed_commitment=sha256(f"concurrent-series-draw-seed-{created.series.id}".encode("utf-8")).hexdigest(),
        idempotency_key=f"concurrent-series-draw-commit-{uuid4()}",
        commit=True,
    )
    return (
        await service.open(
            series_id=created.series.id,
            actor_user_id=admin.id,
            idempotency_key=f"concurrent-series-open-{uuid4()}",
            commit=True,
        )
    ).series


@pytest.mark.asyncio
async def test_parallel_ticket_reservations_cannot_oversell_one_series(
    migrated_postgres_url: str,
) -> None:
    """Two buyers racing for the last ticket yield one order and one safe rejection."""

    engine = create_async_engine(migrated_postgres_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as setup_session:
            admin_role = Role(name=RoleName.ADMIN.value, description="Inventory test admin")
            admin = User(
                email="ticket-concurrency-admin@example.com",
                password_hash=hash_password("correct-horse-battery-staple"),
                roles=[admin_role],
            )
            buyer_a = User(email="ticket-concurrency-a@example.com", password_hash="test-only-hash")
            buyer_b = User(email="ticket-concurrency-b@example.com", password_hash="test-only-hash")
            setup_session.add_all((admin, buyer_a, buyer_b))
            await setup_session.commit()
            series = await _create_open_series(setup_session, admin=admin, ticket_limit=1)

        start = asyncio.Barrier(2)

        async def attempt(buyer_id, key: str):
            async with sessions() as session:
                await start.wait()
                try:
                    return await OrderService(session).create(
                        buyer_user_id=buyer_id,
                        selection=OrderSelection(TicketProductType.SERIES, series.id, 1),
                        idempotency_key=key,
                        commit=True,
                    )
                except ConflictError as error:
                    await session.rollback()
                    return error

        outcomes = await asyncio.wait_for(
            asyncio.gather(
                attempt(buyer_a.id, "ticket-race-buyer-a"),
                attempt(buyer_b.id, "ticket-race-buyer-b"),
            ),
            timeout=15,
        )
        successes = [outcome for outcome in outcomes if not isinstance(outcome, ConflictError)]
        conflicts = [outcome for outcome in outcomes if isinstance(outcome, ConflictError)]
        assert len(successes) == 1
        assert len(conflicts) == 1
        assert conflicts[0].code == "INSUFFICIENT_TICKET_INVENTORY"

        async with sessions() as verification_session:
            stored_series = await verification_session.get(TicketSeries, series.id)
            orders = list(await verification_session.scalars(select(Order).where(Order.buyer_user_id.in_([buyer_a.id, buyer_b.id]))))
            reservations = list(
                await verification_session.scalars(
                    select(TicketReservation).where(TicketReservation.series_id == series.id)
                )
            )
            assert stored_series is not None
            assert (stored_series.sold_count, stored_series.reserved_count) == (0, 1)
            assert len(orders) == 1
            assert len(reservations) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_parallel_settled_allocations_never_duplicate_ticket_serials(
    migrated_postgres_url: str,
) -> None:
    """Series locks serialize ticket serial allocation for separate paid orders."""

    engine = create_async_engine(migrated_postgres_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as setup_session:
            admin_role = Role(name=RoleName.ADMIN.value, description="Allocation test admin")
            admin = User(
                email="serial-concurrency-admin@example.com",
                password_hash=hash_password("correct-horse-battery-staple"),
                roles=[admin_role],
            )
            buyer_a = User(email="serial-concurrency-a@example.com", password_hash="test-only-hash")
            buyer_b = User(email="serial-concurrency-b@example.com", password_hash="test-only-hash")
            setup_session.add_all((admin, buyer_a, buyer_b))
            await setup_session.commit()
            series = await _create_open_series(setup_session, admin=admin, ticket_limit=2)
            order_a = await OrderService(setup_session).create(
                buyer_user_id=buyer_a.id,
                selection=OrderSelection(TicketProductType.SERIES, series.id, 1),
                idempotency_key="serial-order-a-create",
                commit=True,
            )
            order_b = await OrderService(setup_session).create(
                buyer_user_id=buyer_b.id,
                selection=OrderSelection(TicketProductType.SERIES, series.id, 1),
                idempotency_key="serial-order-b-create",
                commit=True,
            )
            settlements = {order_a.order.id: uuid4(), order_b.order.id: uuid4()}
            for order_id, settlement_id in settlements.items():
                order = await setup_session.get(Order, order_id)
                assert order is not None
                order.status = OrderStatus.PAID
                order.settlement_reference_id = settlement_id
                order.settled_at = datetime.now(timezone.utc)
            await setup_session.commit()

        start = asyncio.Barrier(2)

        async def allocate(order_id, settlement_id, key: str):
            async with sessions() as session:
                await start.wait()
                return await TicketAllocationService(session).allocate_paid_order(
                    order_id=order_id,
                    settlement_reference_id=settlement_id,
                    idempotency_key=key,
                    actor_user_id=None,
                    commit=True,
                )

        outcomes = await asyncio.wait_for(
            asyncio.gather(
                allocate(order_a.order.id, settlements[order_a.order.id], "serial-allocate-a"),
                allocate(order_b.order.id, settlements[order_b.order.id], "serial-allocate-b"),
            ),
            timeout=15,
        )
        assert all(len(outcome.tickets) == 1 for outcome in outcomes)
        async with sessions() as verification_session:
            tickets = list(
                await verification_session.scalars(
                    select(Ticket).where(Ticket.series_id == series.id).order_by(Ticket.serial_number)
                )
            )
            stored_series = await verification_session.get(TicketSeries, series.id)
            assert [ticket.serial_number for ticket in tickets] == [1, 2]
            assert len({ticket.serial_number for ticket in tickets}) == 2
            assert stored_series is not None
            assert (stored_series.sold_count, stored_series.reserved_count) == (2, 0)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_parallel_p2p_buyers_claim_one_exact_withdrawal_once(
    migrated_postgres_url: str,
) -> None:
    """The real row locks and partial index allow only one active match per withdrawal."""

    engine = create_async_engine(migrated_postgres_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as setup_session:
            receiver = User(email="p2p-race-receiver@example.com", password_hash="test-only-hash")
            buyer_a = User(email="p2p-race-buyer-a@example.com", password_hash="test-only-hash")
            buyer_b = User(email="p2p-race-buyer-b@example.com", password_hash="test-only-hash")
            setup_session.add_all((receiver, buyer_a, buyer_b))
            await setup_session.commit()
            receiver_id, buyer_a_id, buyer_b_id = receiver.id, buyer_a.id, buyer_b.id
            wallet = (
                await WalletService(setup_session).provision_wallet(
                    user_id=receiver_id,
                    actor_user_id=receiver_id,
                    currency="INR",
                    idempotency_key="p2p-race-provision-001",
                    commit=True,
                )
            ).wallet
            await WalletService(setup_session).credit_available(
                wallet_id=wallet.id,
                actor_user_id=None,
                amount_paise=100_000,
                idempotency_key="p2p-race-credit-001",
                source_type="p2p-race-host-credit",
                source_id=uuid4(),
                reason="test funding",
                commit=True,
            )
            destination = PaymentDestination(
                user_id=receiver_id,
                provider_namespace="upi",
                display_label="R***@upi",
                destination_data={"upi_id": "receiver@upi"},
                status=PaymentDestinationStatus.VERIFIED,
                verification_method="test-controlled-verification",
                verification_evidence_reference="p2p-race-evidence",
                verified_at=datetime.now(timezone.utc),
            )
            setup_session.add(destination)
            await setup_session.commit()
            withdrawal = await WithdrawalService(setup_session).create(
                actor_user_id=receiver_id,
                amount_paise=50_000,
                payment_destination_id=destination.id,
                idempotency_key="p2p-race-withdrawal-001",
                commit=True,
            )
            expiry = datetime.now(timezone.utc) + timedelta(minutes=15)
            order_a = Order(
                buyer_user_id=buyer_a_id,
                status=OrderStatus.PENDING_PAYMENT,
                delivery_status=DeliveryStatus.NOT_STARTED,
                total_paise=50_000,
                currency="INR",
                expires_at=expiry,
            )
            order_b = Order(
                buyer_user_id=buyer_b_id,
                status=OrderStatus.PENDING_PAYMENT,
                delivery_status=DeliveryStatus.NOT_STARTED,
                total_paise=50_000,
                currency="INR",
                expires_at=expiry,
            )
            setup_session.add_all((order_a, order_b))
            await setup_session.commit()
            order_a_id, order_b_id = order_a.id, order_b.id

        barrier = asyncio.Barrier(2)

        async def attempt(order_id, buyer_id, key: str):
            async with sessions() as session:
                await barrier.wait()
                return await P2PService(session).queue_or_match_order(
                    order_id=order_id,
                    buyer_user_id=buyer_id,
                    idempotency_key=key,
                    commit=True,
                )

        outcomes = await asyncio.wait_for(
            asyncio.gather(
                attempt(order_a_id, buyer_a_id, "p2p-race-match-a-001"),
                attempt(order_b_id, buyer_b_id, "p2p-race-match-b-001"),
            ),
            timeout=15,
        )
        assert len([outcome for outcome in outcomes if outcome.match is not None]) == 1

        async with sessions() as verify_session:
            matches = list(
                await verify_session.scalars(
                    select(P2PMatch).where(P2PMatch.withdrawal_id == withdrawal.withdrawal.id)
                )
            )
            stored_withdrawal = await verify_session.get(WithdrawalRequest, withdrawal.withdrawal.id)
            stored_orders = [
                await verify_session.get(Order, order_a_id),
                await verify_session.get(Order, order_b_id),
            ]
            assert len(matches) == 1
            assert matches[0].status == P2PMatchStatus.WAITING_FOR_PAYMENT
            assert stored_withdrawal is not None and stored_withdrawal.status == WithdrawalStatus.MATCHED
            assert all(order is not None for order in stored_orders)
            assert {order.status for order in stored_orders if order is not None} == {
                OrderStatus.AWAITING_PAYMENT,
                OrderStatus.WAITING_FOR_MATCH,
            }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_parallel_p2p_same_reference_claims_are_serialized_into_review(
    migrated_postgres_url: str,
) -> None:
    """A missing-row reference lock prevents two independent claims advancing.

    PostgreSQL cannot lock a row that does not exist. This regression uses two
    separate matches and two sessions to prove the advisory lock makes the
    later raw claim observe the earlier one and enter manual review.
    """

    engine = create_async_engine(migrated_postgres_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    reference = "UTR-PG-PARALLEL-CLAIM-001"
    try:
        async with sessions() as setup_session:
            _receiver_one, buyer_one_id, match_one_id = await _create_matched_p2p_flow(
                setup_session, label="claim-one"
            )
            _receiver_two, buyer_two_id, match_two_id = await _create_matched_p2p_flow(
                setup_session, label="claim-two"
            )

        barrier = asyncio.Barrier(2)

        async def submit(match_id: UUID, buyer_id: UUID, key: str):
            async with sessions() as session:
                await barrier.wait()
                return await P2PService(session).submit_payment(
                    match_id=match_id,
                    buyer_user_id=buyer_id,
                    provider_namespace="upi",
                    claimed_reference=reference,
                    observed_amount_paise=50_000,
                    observed_currency="INR",
                    declared_paid_at=datetime.now(timezone.utc),
                    evidence_upload_id=None,
                    provider_evidence_reference=None,
                    evidence_metadata=None,
                    idempotency_key=key,
                    commit=True,
                )

        outcomes = await asyncio.wait_for(
            asyncio.gather(
                submit(match_one_id, buyer_one_id, "p2p-parallel-claim-one-001"),
                submit(match_two_id, buyer_two_id, "p2p-parallel-claim-two-001"),
                return_exceptions=True,
            ),
            timeout=20,
        )
        assert not [outcome for outcome in outcomes if isinstance(outcome, Exception)]

        async with sessions() as verify_session:
            submissions = list(
                await verify_session.scalars(
                    select(P2PPaymentSubmission).where(
                        P2PPaymentSubmission.provider_namespace == "upi",
                        P2PPaymentSubmission.claimed_reference == reference,
                    )
                )
            )
            matches = [
                await verify_session.get(P2PMatch, match_one_id),
                await verify_session.get(P2PMatch, match_two_id),
            ]
            assert len(submissions) == 2
            assert {submission.verification_status for submission in submissions} == {
                PaymentSubmissionVerificationStatus.UNVERIFIED,
                PaymentSubmissionVerificationStatus.MANUAL_REVIEW,
            }
            assert {match.status for match in matches if match is not None} == {
                P2PMatchStatus.WAITING_FOR_RECEIVER_CONFIRMATION,
                P2PMatchStatus.UNDER_VERIFICATION,
            }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_parallel_verified_reference_race_returns_review_not_integrity_error(
    migrated_postgres_url: str,
) -> None:
    """Legacy duplicate claims cannot race into two verified-reference inserts."""

    engine = create_async_engine(migrated_postgres_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    reference = "UTR-PG-PARALLEL-VERIFY-001"
    try:
        async with sessions() as setup_session:
            _receiver_one, buyer_one_id, match_one_id = await _create_matched_p2p_flow(
                setup_session, label="verify-one"
            )
            _receiver_two, buyer_two_id, match_two_id = await _create_matched_p2p_flow(
                setup_session, label="verify-two"
            )
            admin_role = Role(name=RoleName.ADMIN.value, description="P2P concurrency admin")
            admin = User(
                email="p2p-reference-race-admin@example.com",
                password_hash="test-only-hash",
                roles=[admin_role],
            )
            setup_session.add(admin)
            submission_one = P2PPaymentSubmission(
                id=uuid4(),
                match_id=match_one_id,
                buyer_user_id=buyer_one_id,
                provider_namespace="upi",
                claimed_reference=reference,
                expected_amount_paise=50_000,
                expected_currency="INR",
                observed_amount_paise=50_000,
                observed_currency="INR",
                declared_paid_at=datetime.now(timezone.utc),
                verification_status=PaymentSubmissionVerificationStatus.UNVERIFIED,
                is_late=False,
            )
            submission_two = P2PPaymentSubmission(
                id=uuid4(),
                match_id=match_two_id,
                buyer_user_id=buyer_two_id,
                provider_namespace="upi",
                claimed_reference=reference,
                expected_amount_paise=50_000,
                expected_currency="INR",
                observed_amount_paise=50_000,
                observed_currency="INR",
                declared_paid_at=datetime.now(timezone.utc),
                verification_status=PaymentSubmissionVerificationStatus.UNVERIFIED,
                is_late=False,
            )
            setup_session.add_all((submission_one, submission_two))
            await setup_session.commit()
            admin_id = admin.id

        barrier = asyncio.Barrier(2)

        async def settle(match_id: UUID, submission_id: UUID, key: str):
            async with sessions() as session:
                await barrier.wait()
                return await P2PService(session).admin_settle(
                    match_id=match_id,
                    actor_user_id=admin_id,
                    payment_submission_id=submission_id,
                    reason="Parallel verified-reference regression coverage",
                    evidence_reference=f"{key}-evidence",
                    verification_source="MANUAL_RECONCILIATION",
                    dispute_id=None,
                    idempotency_key=key,
                    commit=True,
                )

        outcomes = await asyncio.wait_for(
            asyncio.gather(
                settle(match_one_id, submission_one.id, "p2p-parallel-verify-one-001"),
                settle(match_two_id, submission_two.id, "p2p-parallel-verify-two-001"),
                return_exceptions=True,
            ),
            timeout=20,
        )
        assert not [outcome for outcome in outcomes if isinstance(outcome, Exception)]
        payloads = [outcome.response_payload for outcome in outcomes]
        assert sum("settlement" in payload for payload in payloads) == 1
        assert sum(payload.get("code") == "REFERENCE_CONFLICT" for payload in payloads) == 1

        async with sessions() as verify_session:
            verified_count = await verify_session.scalar(
                select(func.count()).select_from(P2PVerifiedPaymentReference)
            )
            settlement_count = await verify_session.scalar(select(func.count()).select_from(P2PSettlement))
            matches = [
                await verify_session.get(P2PMatch, match_one_id),
                await verify_session.get(P2PMatch, match_two_id),
            ]
            assert verified_count == 1
            assert settlement_count == 1
            assert {match.status for match in matches if match is not None} == {
                P2PMatchStatus.SETTLED,
                P2PMatchStatus.ADMIN_REVIEW,
            }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_parallel_p2p_withdrawal_creates_one_request_and_hold(
    migrated_postgres_url: str,
) -> None:
    """A wallet lock plus the unresolved-request index serializes two creates."""

    engine = create_async_engine(migrated_postgres_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as setup_session:
            receiver = User(email="p2p-withdrawal-race@example.com", password_hash="test-only-hash")
            setup_session.add(receiver)
            await setup_session.commit()
            receiver_id = receiver.id
            wallet = (
                await WalletService(setup_session).provision_wallet(
                    user_id=receiver_id,
                    actor_user_id=receiver_id,
                    currency="INR",
                    idempotency_key="p2p-withdrawal-race-provision-001",
                    commit=True,
                )
            ).wallet
            await WalletService(setup_session).credit_available(
                wallet_id=wallet.id,
                actor_user_id=None,
                amount_paise=100_000,
                idempotency_key="p2p-withdrawal-race-credit-001",
                source_type="p2p-withdrawal-race-host-credit",
                source_id=uuid4(),
                reason="test funding",
                commit=True,
            )
            destination = PaymentDestination(
                user_id=receiver_id,
                provider_namespace="upi",
                display_label="R***@upi",
                destination_data={"upi_id": "receiver@upi"},
                status=PaymentDestinationStatus.VERIFIED,
                verification_method="test-controlled-verification",
                verification_evidence_reference="p2p-withdrawal-race-evidence",
                verified_at=datetime.now(timezone.utc),
            )
            setup_session.add(destination)
            await setup_session.commit()
            wallet_id, destination_id = wallet.id, destination.id

        barrier = asyncio.Barrier(2)

        async def attempt(key: str):
            async with sessions() as session:
                await barrier.wait()
                return await WithdrawalService(session).create(
                    actor_user_id=receiver_id,
                    amount_paise=50_000,
                    payment_destination_id=destination_id,
                    idempotency_key=key,
                    commit=True,
                )

        outcomes = await asyncio.wait_for(
            asyncio.gather(
                attempt("p2p-withdrawal-race-create-a-001"),
                attempt("p2p-withdrawal-race-create-b-001"),
                return_exceptions=True,
            ),
            timeout=15,
        )
        assert len([outcome for outcome in outcomes if not isinstance(outcome, Exception)]) == 1
        assert len([outcome for outcome in outcomes if isinstance(outcome, ConflictError)]) == 1

        async with sessions() as verify_session:
            withdrawals = list(
                await verify_session.scalars(
                    select(WithdrawalRequest).where(WithdrawalRequest.user_id == receiver_id)
                )
            )
            holds = list(
                await verify_session.scalars(select(WalletHold).where(WalletHold.wallet_id == wallet_id))
            )
            stored_wallet = await verify_session.get(Wallet, wallet_id)
            assert len(withdrawals) == 1
            assert withdrawals[0].status == WithdrawalStatus.WAITING_FOR_BUYER
            assert len(holds) == 1 and holds[0].status == WalletHoldStatus.ACTIVE
            assert stored_wallet is not None and (stored_wallet.available_paise, stored_wallet.locked_paise) == (50_000, 50_000)
    finally:
        await engine.dispose()
