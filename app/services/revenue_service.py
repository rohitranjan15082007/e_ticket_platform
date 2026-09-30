"""Settlement-side immutable revenue allocation and bounded reporting."""

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditService
from app.core.idempotency import IdempotencyService, canonical_payload, fingerprint
from app.core.money import require_inr_currency, require_paise
from app.exceptions import ConflictError, InvariantViolationError, ValidationError
from app.models.ledger import AccountKind, PostingDirection
from app.models.order import Order, OrderStatus
from app.models.revenue_allocation import RevenueAllocation, RevenueAllocationSource
from app.models.user import IdempotencyRecord
from app.services.ledger_service import LedgerService, PostingDraft


PRIZE_POOL_BPS = 4_500
MARKETING_BPS = 2_000
OPERATIONS_BPS = 1_000
EMERGENCY_RESERVE_BPS = 500
TOTAL_BPS = 10_000


@dataclass(slots=True)
class RevenueAllocationResult:
    allocation: RevenueAllocation
    response_payload: dict[str, object]
    replayed: bool


class RevenueService:
    """Posts a single durable split from a settlement pending-funds account."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.ledger = LedgerService(session)
        self.audit = AuditService(session)
        self.idempotency = IdempotencyService(session)

    async def allocate_settled_order(
        self,
        *,
        order: Order,
        settlement_reference_id: UUID,
        source: RevenueAllocationSource,
        actor_user_id: UUID | None,
        commit: bool,
    ) -> RevenueAllocationResult:
        """Allocate a trusted settlement exactly once in its existing transaction.

        This method is intentionally an internal composition primitive. Both
        verified settlement workflows call it while they still hold the order
        lock; delivery retries and background jobs must never create revenue.
        """

        if not isinstance(settlement_reference_id, UUID):
            raise ValidationError("INVALID_SETTLEMENT_REFERENCE", "Settlement reference must be a UUID")
        if not isinstance(source, RevenueAllocationSource):
            raise ValidationError("INVALID_REVENUE_SOURCE", "Revenue source is invalid")
        if order.status not in {OrderStatus.PAID, OrderStatus.FULFILLED}:
            raise ConflictError("ORDER_NOT_SETTLED", "Revenue can be allocated only for a paid order")
        if order.settlement_reference_id != settlement_reference_id or order.settled_at is None:
            raise ConflictError("UNVERIFIED_SETTLEMENT", "Order settlement reference is not durable")
        base = require_paise(order.total_paise, field="order.total_paise")
        currency = require_inr_currency(order.currency)
        split = self.split_paise(base)
        scope = f"settlement:{settlement_reference_id}:revenue-allocation"
        key = f"revenue-allocation:{settlement_reference_id}"
        request_fingerprint = fingerprint(
            {
                "order_id": order.id,
                "settlement_reference_id": settlement_reference_id,
                "source": source,
                "allocation_base_paise": base,
                "currency": currency,
                **split,
            }
        )
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay(replay)
        existing = await self.session.scalar(
            select(RevenueAllocation)
            .where(
                or_(
                    RevenueAllocation.order_id == order.id,
                    RevenueAllocation.settlement_reference_id == settlement_reference_id,
                )
            )
            .with_for_update()
        )
        if existing is not None:
            if (
                existing.order_id == order.id
                and existing.settlement_reference_id == settlement_reference_id
                and existing.source == source
                and existing.allocation_base_paise == base
            ):
                return RevenueAllocationResult(existing, self.snapshot(existing), True)
            raise InvariantViolationError("Existing revenue allocation conflicts with this settlement")

        source_account = await self._source_account(source, currency)
        prize_pool = await self.ledger.get_revenue_bucket_account(
            kind=AccountKind.PLATFORM_PRIZE_POOL, currency=currency
        )
        marketing = await self.ledger.get_revenue_bucket_account(
            kind=AccountKind.PLATFORM_MARKETING, currency=currency
        )
        operations = await self.ledger.get_revenue_bucket_account(
            kind=AccountKind.PLATFORM_OPERATIONS, currency=currency
        )
        reserve = await self.ledger.get_revenue_bucket_account(
            kind=AccountKind.PLATFORM_EMERGENCY_RESERVE, currency=currency
        )
        profit_growth = await self.ledger.get_revenue_bucket_account(
            kind=AccountKind.PLATFORM_PROFIT_GROWTH, currency=currency
        )

        allocation_id = uuid4()
        record = self.idempotency.record(
            actor_scope=scope,
            key=key,
            request_fingerprint=request_fingerprint,
            resource_type="revenue_allocation",
            resource_id=allocation_id,
        )
        await self.session.flush()
        bucket_postings = [
            PostingDraft(prize_pool.id, PostingDirection.CREDIT, split["prize_pool_paise"], currency),
            PostingDraft(marketing.id, PostingDirection.CREDIT, split["marketing_paise"], currency),
            PostingDraft(operations.id, PostingDirection.CREDIT, split["operations_paise"], currency),
            PostingDraft(reserve.id, PostingDirection.CREDIT, split["reserve_paise"], currency),
            PostingDraft(
                profit_growth.id, PostingDirection.CREDIT, split["profit_growth_paise"], currency
            ),
        ]
        journal = await self.ledger.post_balanced_group(
            event_type="ORDER_REVENUE_ALLOCATED",
            currency=currency,
            reference_type="revenue_allocation",
            reference_id=allocation_id,
            actor_user_id=actor_user_id,
            reason=(
                "Integer-paise order revenue allocation; deterministic remainder "
                "is assigned to the profit and growth fund"
            ),
            postings=[
                PostingDraft(source_account.id, PostingDirection.DEBIT, base, currency),
                *[posting for posting in bucket_postings if posting.amount_paise > 0],
            ],
            idempotency_record_id=record.id,
        )
        allocation = RevenueAllocation(
            id=allocation_id,
            order_id=order.id,
            settlement_reference_id=settlement_reference_id,
            source=source,
            source_account_id=source_account.id,
            journal_group_id=journal.id,
            idempotency_record_id=record.id,
            allocation_base_paise=base,
            prize_pool_paise=split["prize_pool_paise"],
            marketing_paise=split["marketing_paise"],
            operations_paise=split["operations_paise"],
            reserve_paise=split["reserve_paise"],
            profit_growth_paise=split["profit_growth_paise"],
            currency=currency,
            allocated_by_user_id=actor_user_id,
            allocated_at=datetime.now(timezone.utc),
        )
        self.session.add(allocation)
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="revenue_allocation",
            entity_id=allocation.id,
            action="ORDER_REVENUE_ALLOCATED",
            before_state=None,
            after_state=self.allocation_state(allocation),
        )
        await self.session.flush()
        response = self.snapshot(allocation)
        record.response_payload = response
        await self._finish(commit=commit)
        return RevenueAllocationResult(allocation, response, False)

    async def list_allocations(
        self,
        *,
        limit: int,
        starts_at: datetime | None = None,
        ends_at: datetime | None = None,
    ) -> list[RevenueAllocation]:
        """Read a bounded, newest-first allocation list for administrators."""

        limit = self._limit(limit)
        start, end = self._window(starts_at, ends_at)
        statement = select(RevenueAllocation)
        if start is not None:
            statement = statement.where(RevenueAllocation.allocated_at >= start)
        if end is not None:
            statement = statement.where(RevenueAllocation.allocated_at < end)
        return list(
            await self.session.scalars(
                statement.order_by(RevenueAllocation.allocated_at.desc(), RevenueAllocation.id).limit(limit)
            )
        )

    async def report(
        self, *, starts_at: datetime | None = None, ends_at: datetime | None = None
    ) -> dict[str, object]:
        """Aggregate posted immutable records only; never infer unposted revenue."""

        start, end = self._window(starts_at, ends_at)
        statement = select(
            func.count(RevenueAllocation.id),
            func.coalesce(func.sum(RevenueAllocation.allocation_base_paise), 0),
            func.coalesce(func.sum(RevenueAllocation.prize_pool_paise), 0),
            func.coalesce(func.sum(RevenueAllocation.marketing_paise), 0),
            func.coalesce(func.sum(RevenueAllocation.operations_paise), 0),
            func.coalesce(func.sum(RevenueAllocation.reserve_paise), 0),
            func.coalesce(func.sum(RevenueAllocation.profit_growth_paise), 0),
        )
        if start is not None:
            statement = statement.where(RevenueAllocation.allocated_at >= start)
        if end is not None:
            statement = statement.where(RevenueAllocation.allocated_at < end)
        row = (await self.session.execute(statement)).one()
        return canonical_payload(
            {
                "starts_at": self._datetime(start),
                "ends_at": self._datetime(end),
                "currency": "INR",
                "allocation_count": int(row[0] or 0),
                "allocation_base_paise": int(row[1] or 0),
                "prize_pool_paise": int(row[2] or 0),
                "marketing_paise": int(row[3] or 0),
                "operations_paise": int(row[4] or 0),
                "reserve_paise": int(row[5] or 0),
                "profit_growth_paise": int(row[6] or 0),
            }
        )

    async def _source_account(
        self, source: RevenueAllocationSource, currency: str
    ):
        if source == RevenueAllocationSource.EXTERNAL_ORDER_PENDING:
            return await self.ledger.get_external_order_pending_account(currency)
        if source == RevenueAllocationSource.P2P_ORDER_PENDING:
            return await self.ledger.get_p2p_order_pending_account(currency)
        raise ValidationError("INVALID_REVENUE_SOURCE", "Revenue source is invalid")

    async def _replay(self, record: IdempotencyRecord) -> RevenueAllocationResult:
        if record.resource_id is None or not isinstance(record.response_payload, dict):
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior revenue allocation cannot be recovered")
        allocation = await self.session.get(RevenueAllocation, record.resource_id)
        if allocation is None:
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior revenue allocation no longer exists")
        return RevenueAllocationResult(allocation, dict(record.response_payload), True)

    @staticmethod
    def split_paise(base_paise: int) -> dict[str, int]:
        """Calculate the documented split and give every rounding paise to profit."""

        base = require_paise(base_paise, field="allocation_base_paise")
        prize_pool = base * PRIZE_POOL_BPS // TOTAL_BPS
        marketing = base * MARKETING_BPS // TOTAL_BPS
        operations = base * OPERATIONS_BPS // TOTAL_BPS
        reserve = base * EMERGENCY_RESERVE_BPS // TOTAL_BPS
        profit_growth = base - prize_pool - marketing - operations - reserve
        if profit_growth <= 0:
            raise InvariantViolationError("Revenue split leaves no profit and growth allocation")
        return {
            "prize_pool_paise": prize_pool,
            "marketing_paise": marketing,
            "operations_paise": operations,
            "reserve_paise": reserve,
            "profit_growth_paise": profit_growth,
        }

    @staticmethod
    def allocation_state(allocation: RevenueAllocation) -> dict[str, object]:
        return {
            "id": allocation.id,
            "order_id": allocation.order_id,
            "settlement_reference_id": allocation.settlement_reference_id,
            "source": allocation.source,
            "source_account_id": allocation.source_account_id,
            "journal_group_id": allocation.journal_group_id,
            "idempotency_record_id": allocation.idempotency_record_id,
            "allocation_base_paise": allocation.allocation_base_paise,
            "prize_pool_paise": allocation.prize_pool_paise,
            "marketing_paise": allocation.marketing_paise,
            "operations_paise": allocation.operations_paise,
            "reserve_paise": allocation.reserve_paise,
            "profit_growth_paise": allocation.profit_growth_paise,
            "currency": allocation.currency,
            "allocated_by_user_id": allocation.allocated_by_user_id,
            "allocated_at": RevenueService._datetime(allocation.allocated_at),
        }

    @classmethod
    def snapshot(cls, allocation: RevenueAllocation) -> dict[str, object]:
        return canonical_payload(cls.allocation_state(allocation))

    @staticmethod
    def _limit(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 1_000:
            raise ValidationError("INVALID_LIMIT", "limit must be an integer between 1 and 1000")
        return value

    @staticmethod
    def _window(
        starts_at: datetime | None, ends_at: datetime | None
    ) -> tuple[datetime | None, datetime | None]:
        for name, value in (("starts_at", starts_at), ("ends_at", ends_at)):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValidationError("INVALID_REPORT_WINDOW", f"{name} must include a timezone")
        start = starts_at.astimezone(timezone.utc) if starts_at is not None else None
        end = ends_at.astimezone(timezone.utc) if ends_at is not None else None
        if start is not None and end is not None and end <= start:
            raise ValidationError("INVALID_REPORT_WINDOW", "ends_at must be after starts_at")
        return start, end

    @staticmethod
    def _datetime(value: datetime | None) -> str | None:
        if value is None:
            return None
        return (value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)).isoformat()

    async def _finish(self, *, commit: bool) -> None:
        await self.session.flush()
        if commit:
            await self.session.commit()
