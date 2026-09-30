"""Settlement-gated, idempotent ticket entitlement allocation.

This module deliberately has no HTTP endpoint and never declares an order paid.
Phase 4's verified settlement transaction must persist the trusted settlement
reference, move the order to ``PAID``, and invoke this service in that same
unit of work (or with an equivalent durable delivery job).
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditService
from app.core.idempotency import IdempotencyService, canonical_payload, fingerprint
from app.core.state_machine import ORDER_TRANSITIONS, require_transition
from app.exceptions import ConflictError, ValidationError
from app.models.order import DeliveryStatus, Order, OrderStatus, TicketProductType
from app.models.ticket import Ticket
from app.models.ticket_package import TicketPackage
from app.models.user import IdempotencyRecord
from app.repositories.order_repository import get_order
from app.repositories.ticket_repository import get_ticket_package
from app.services.inventory_service import InventoryService
from app.services.order_service import OrderService


@dataclass(slots=True)
class TicketAllocationResult:
    order: Order
    tickets: list[Ticket]
    response_payload: dict[str, object]
    replayed: bool


class TicketAllocationService:
    """Allocate once only after a trusted upstream settlement marks an order PAID."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.audit = AuditService(session)
        self.idempotency = IdempotencyService(session)
        self.inventory = InventoryService(session)

    async def allocate_paid_order(
        self,
        *,
        order_id: UUID,
        settlement_reference_id: UUID,
        idempotency_key: str,
        actor_user_id: UUID | None,
        commit: bool,
    ) -> TicketAllocationResult:
        """Convert active reservations to tickets after verified settlement only.

        ``settlement_reference_id`` must already be the durable reference saved
        by an authorized payment/settlement workflow.  Supplying an ID here does
        not verify a payment and cannot turn an unpaid order into a paid one.
        """

        if not isinstance(settlement_reference_id, UUID):
            raise ValidationError("INVALID_SETTLEMENT_REFERENCE", "Settlement reference must be a UUID")
        order = await self._locked_order(order_id)
        scope = f"settlement:{settlement_reference_id}:ticket-allocation"
        request_fingerprint = fingerprint(
            {"order_id": order_id, "settlement_reference_id": settlement_reference_id}
        )
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay(replay)
        if order.status != OrderStatus.PAID:
            raise ConflictError("ORDER_NOT_SETTLED", "Tickets can be allocated only after a PAID order")
        if order.settlement_reference_id != settlement_reference_id or order.settled_at is None:
            raise ConflictError("UNVERIFIED_SETTLEMENT", "Order has no matching trusted settlement reference")
        # A failed prior delivery is explicitly retryable.  The settlement is
        # already durable, so this only resumes entitlement allocation and can
        # never create another debit or settlement.
        if order.delivery_status not in {
            DeliveryStatus.NOT_STARTED,
            DeliveryStatus.PENDING,
            DeliveryStatus.FAILED,
        }:
            raise ConflictError("ORDER_DELIVERY_IN_PROGRESS", "Order delivery cannot be allocated in its current state")
        if len(order.items) != 1:
            raise ConflictError("ORDER_INCONSISTENT", "Phase 3 orders must have exactly one order item")
        item = order.items[0]
        package = await self._package_for_item(item.product_type, item.product_id)
        before_order = OrderService.snapshot(order)
        before_package = self._package_state(package) if package is not None else None
        if package is not None:
            if package.reserved_count < item.quantity:
                raise ConflictError("INVENTORY_INCONSISTENT", "Package reservation counter is inconsistent")
            package.reserved_count -= item.quantity
            package.sold_count += item.quantity
        order.delivery_status = DeliveryStatus.PROCESSING
        tickets = await self.inventory.allocate(order=order, actor_user_id=actor_user_id)
        require_transition(
            current=order.status, target=OrderStatus.FULFILLED, transitions=ORDER_TRANSITIONS, resource="Order"
        )
        order.status = OrderStatus.FULFILLED
        order.delivery_status = DeliveryStatus.DELIVERED
        record = self.idempotency.record(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint,
            resource_type="ticket_allocation", resource_id=order.id,
        )
        self.audit.record(
            actor_user_id=actor_user_id, entity_type="order", entity_id=order.id,
            action="ORDER_TICKETS_FULFILLED", before_state=before_order,
            after_state=OrderService._order_state(order),
        )
        if package is not None and before_package is not None:
            self.audit.record(
                actor_user_id=actor_user_id, entity_type="ticket_package", entity_id=package.id,
                action="TICKET_PACKAGE_SOLD", before_state=before_package,
                after_state=self._package_state(package),
            )
        await self.session.flush()
        response = self._snapshot(order, tickets)
        record.response_payload = response
        await self._finish(commit=commit)
        return TicketAllocationResult(order, tickets, response, False)

    async def _locked_order(self, order_id: UUID) -> Order:
        order = await get_order(self.session, order_id, for_update=True)
        if order is None:
            raise ValidationError("UNKNOWN_ORDER", "Order does not exist")
        return order

    async def _package_for_item(
        self, product_type: TicketProductType, product_id: UUID
    ) -> TicketPackage | None:
        if product_type == TicketProductType.SERIES:
            return None
        if product_type != TicketProductType.PACKAGE:
            raise ConflictError("ORDER_INCONSISTENT", "Order item has an unknown product type")
        package = await get_ticket_package(self.session, product_id, for_update=True)
        if package is None:
            raise ConflictError("ORDER_INCONSISTENT", "Package backing this order no longer exists")
        return package

    async def _replay(self, record: IdempotencyRecord) -> TicketAllocationResult:
        if record.resource_id is None:
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior ticket allocation has no order")
        order = await get_order(self.session, record.resource_id)
        if order is None or not isinstance(record.response_payload, dict):
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior ticket allocation cannot be recovered")
        ticket_values = record.response_payload.get("tickets")
        if not isinstance(ticket_values, list):
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior ticket allocation response is incomplete")
        try:
            ticket_ids = [UUID(str(value["id"])) for value in ticket_values if isinstance(value, dict)]
        except (KeyError, TypeError, ValueError) as error:
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior ticket allocation response is invalid") from error
        if len(ticket_ids) != len(ticket_values) or len(set(ticket_ids)) != len(ticket_ids):
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior ticket allocation response is invalid")
        stored = list(await self.session.scalars(select(Ticket).where(Ticket.id.in_(ticket_ids))))
        tickets_by_id = {ticket.id: ticket for ticket in stored}
        if len(tickets_by_id) != len(ticket_ids):
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior allocated tickets cannot be recovered")
        return TicketAllocationResult(
            order,
            [tickets_by_id[ticket_id] for ticket_id in ticket_ids],
            dict(record.response_payload),
            True,
        )

    async def _finish(self, *, commit: bool) -> None:
        await self.session.flush()
        if commit:
            await self.session.commit()

    @staticmethod
    def _package_state(package: TicketPackage) -> dict[str, object]:
        return {
            "id": package.id,
            "sold_count": package.sold_count,
            "reserved_count": package.reserved_count,
            "inventory_limit": package.inventory_limit,
            "is_active": package.is_active,
        }

    @staticmethod
    def _ticket_state(ticket: Ticket) -> dict[str, object]:
        return {
            "id": ticket.id,
            "series_id": ticket.series_id,
            "serial_number": ticket.serial_number,
            "order_item_id": ticket.order_item_id,
            "owner_user_id": ticket.owner_user_id,
            "status": ticket.status,
            "is_winner": ticket.is_winner,
            "prize_paise": ticket.prize_paise,
        }

    @classmethod
    def _snapshot(cls, order: Order, tickets: list[Ticket]) -> dict[str, object]:
        response = OrderService._order_state(order)
        response["tickets"] = [cls._ticket_state(ticket) for ticket in tickets]
        return canonical_payload(response)
