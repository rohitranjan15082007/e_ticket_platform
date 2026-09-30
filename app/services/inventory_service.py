"""Finite ticket inventory reservations and post-settlement allocation."""

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditService
from app.exceptions import ConflictError, ValidationError
from app.models.order import Order, OrderItem, TicketReservation, TicketReservationStatus
from app.models.ticket import Ticket, TicketStatus
from app.models.ticket_series import TicketSeries, TicketSeriesStatus
from app.repositories.ticket_repository import get_ticket_series_locked_many, next_ticket_serial_start


@dataclass(frozen=True, slots=True)
class ReservationPlan:
    series_id: UUID
    quantity: int


class InventoryService:
    """Changes counters only while all affected series rows are locked in one order.

    This service deliberately never commits.  The enclosing order creation,
    cancellation, or settlement transaction decides whether the whole unit of
    work becomes durable.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.audit = AuditService(session)

    async def reserve(
        self,
        *,
        order: Order,
        order_item: OrderItem,
        plans: list[ReservationPlan],
        expires_at: datetime,
        actor_user_id: UUID,
    ) -> list[TicketReservation]:
        """Claim finite stock only while the series is OPEN and in its sales window."""

        normalized = self._normalize_plans(plans)
        expires_at = self._aware(expires_at, field="expires_at")
        series_by_id = await self._locked_series_by_id([plan.series_id for plan in normalized])
        now = datetime.now(timezone.utc)
        reservations: list[TicketReservation] = []
        for plan in normalized:
            series = series_by_id[plan.series_id]
            self._require_sale_open(series, now=now)
            available = series.ticket_limit - series.sold_count - series.reserved_count
            if available < plan.quantity:
                raise ConflictError(
                    "INSUFFICIENT_TICKET_INVENTORY",
                    f"Only {available} tickets remain in series {series.id}",
                )
        for plan in normalized:
            series = series_by_id[plan.series_id]
            before_series = self._series_state(series)
            series.reserved_count += plan.quantity
            reservation = TicketReservation(
                id=uuid4(),
                order_id=order.id,
                order_item_id=order_item.id,
                series_id=series.id,
                order=order,
                order_item=order_item,
                series=series,
                quantity=plan.quantity,
                status=TicketReservationStatus.RESERVED,
                expires_at=expires_at,
            )
            self.session.add(reservation)
            reservations.append(reservation)
            self.audit.record(
                actor_user_id=actor_user_id,
                entity_type="ticket_series",
                entity_id=series.id,
                action="TICKET_INVENTORY_RESERVED",
                before_state=before_series,
                after_state=self._series_state(series),
            )
            self.audit.record(
                actor_user_id=actor_user_id,
                entity_type="ticket_reservation",
                entity_id=reservation.id,
                action="TICKET_RESERVATION_CREATED",
                before_state=None,
                after_state=self._reservation_state(reservation),
            )
        return reservations

    async def release(
        self, *, order: Order, actor_user_id: UUID | None, reason: str
    ) -> list[TicketReservation]:
        """Release exactly the active reservations attached to an already-locked order."""

        active = [
            reservation
            for reservation in order.reservations
            if reservation.status == TicketReservationStatus.RESERVED
        ]
        if not active:
            return []
        reason = self._text(reason, field="reason", maximum=500)
        series_by_id = await self._locked_series_by_id([reservation.series_id for reservation in active])
        now = datetime.now(timezone.utc)
        for reservation in active:
            series = series_by_id[reservation.series_id]
            if series.reserved_count < reservation.quantity:
                raise ConflictError("INVENTORY_INCONSISTENT", "Reserved counter is below the active reservation")
            before_series = self._series_state(series)
            before_reservation = self._reservation_state(reservation)
            series.reserved_count -= reservation.quantity
            reservation.status = TicketReservationStatus.RELEASED
            reservation.released_at = now
            self.audit.record(
                actor_user_id=actor_user_id,
                entity_type="ticket_series",
                entity_id=series.id,
                action="TICKET_INVENTORY_RELEASED",
                before_state=before_series,
                after_state=self._series_state(series),
                reason=reason,
            )
            self.audit.record(
                actor_user_id=actor_user_id,
                entity_type="ticket_reservation",
                entity_id=reservation.id,
                action="TICKET_RESERVATION_RELEASED",
                before_state=before_reservation,
                after_state=self._reservation_state(reservation),
                reason=reason,
            )
        return active

    async def allocate(
        self, *, order: Order, actor_user_id: UUID | None
    ) -> list[Ticket]:
        """Consume active reservations and create unique serials after trusted settlement.

        Callers must lock ``order`` and enforce settlement facts before entering
        this method.  The series lock serializes serial-number allocation and
        preserves the counter invariant even under concurrent buyers.
        """

        active = [
            reservation
            for reservation in order.reservations
            if reservation.status == TicketReservationStatus.RESERVED
        ]
        if not active:
            raise ConflictError("NO_ACTIVE_TICKET_RESERVATION", "Order has no reservable ticket inventory")
        series_by_id = await self._locked_series_by_id([reservation.series_id for reservation in active])
        now = datetime.now(timezone.utc)
        tickets: list[Ticket] = []
        for reservation in sorted(active, key=lambda value: str(value.series_id)):
            series = series_by_id[reservation.series_id]
            if series.reserved_count < reservation.quantity:
                raise ConflictError("INVENTORY_INCONSISTENT", "Reserved counter is below the active reservation")
            before_series = self._series_state(series)
            before_reservation = self._reservation_state(reservation)
            first_serial = await next_ticket_serial_start(self.session, series.id)
            series.reserved_count -= reservation.quantity
            series.sold_count += reservation.quantity
            reservation.status = TicketReservationStatus.ALLOCATED
            reservation.allocated_at = now
            for serial_number in range(first_serial, first_serial + reservation.quantity):
                ticket = Ticket(
                    id=uuid4(),
                    series_id=series.id,
                    series=series,
                    serial_number=serial_number,
                    order_item_id=reservation.order_item_id,
                    order_item=reservation.order_item,
                    owner_user_id=order.buyer_user_id,
                    status=TicketStatus.ALLOCATED,
                    is_winner=False,
                    prize_paise=0,
                )
                self.session.add(ticket)
                tickets.append(ticket)
                self.audit.record(
                    actor_user_id=actor_user_id,
                    entity_type="ticket",
                    entity_id=ticket.id,
                    action="TICKET_ALLOCATED",
                    before_state=None,
                    after_state=self._ticket_state(ticket),
                )
            self.audit.record(
                actor_user_id=actor_user_id,
                entity_type="ticket_series",
                entity_id=series.id,
                action="TICKET_INVENTORY_SOLD",
                before_state=before_series,
                after_state=self._series_state(series),
            )
            self.audit.record(
                actor_user_id=actor_user_id,
                entity_type="ticket_reservation",
                entity_id=reservation.id,
                action="TICKET_RESERVATION_ALLOCATED",
                before_state=before_reservation,
                after_state=self._reservation_state(reservation),
            )
        return tickets

    async def _locked_series_by_id(self, series_ids: list[UUID]) -> dict[UUID, TicketSeries]:
        series = await get_ticket_series_locked_many(self.session, series_ids)
        result = {value.id: value for value in series}
        if len(result) != len(set(series_ids)):
            raise ValidationError("UNKNOWN_TICKET_SERIES", "A ticket series does not exist")
        return result

    @classmethod
    def _normalize_plans(cls, plans: object) -> list[ReservationPlan]:
        if not isinstance(plans, list) or not plans:
            raise ValidationError("INVALID_RESERVATION", "At least one ticket series must be reserved")
        result: list[ReservationPlan] = []
        seen: set[UUID] = set()
        for plan in plans:
            if not isinstance(plan, ReservationPlan) or not isinstance(plan.series_id, UUID):
                raise ValidationError("INVALID_RESERVATION", "Reservation plans require a UUID series_id")
            if plan.series_id in seen:
                raise ValidationError("INVALID_RESERVATION", "Each series may be reserved once per order")
            if isinstance(plan.quantity, bool) or not isinstance(plan.quantity, int) or plan.quantity <= 0:
                raise ValidationError("INVALID_RESERVATION", "Reservation quantity must be a positive integer")
            seen.add(plan.series_id)
            result.append(plan)
        return sorted(result, key=lambda value: str(value.series_id))

    @classmethod
    def _require_sale_open(cls, series: TicketSeries, *, now: datetime) -> None:
        if series.status != TicketSeriesStatus.OPEN:
            raise ConflictError("TICKET_SERIES_NOT_OPEN", "Ticket series is not open for sales")
        if now < cls._aware(series.sales_start_at, field="sales_start_at") or now >= cls._aware(series.sales_end_at, field="sales_end_at"):
            raise ConflictError("SERIES_SALES_WINDOW_CLOSED", "Ticket series is outside its sales window")

    @staticmethod
    def _aware(value: datetime, *, field: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _text(value: object, *, field: str, maximum: int) -> str:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
            raise ValidationError("INVALID_RESERVATION", f"{field} must be a non-empty string up to {maximum} characters")
        return value.strip()

    @classmethod
    def _series_state(cls, series: TicketSeries) -> dict[str, object]:
        return {
            "id": series.id,
            "sold_count": series.sold_count,
            "reserved_count": series.reserved_count,
            "ticket_limit": series.ticket_limit,
            "status": series.status,
        }

    @classmethod
    def _reservation_state(cls, reservation: TicketReservation) -> dict[str, object]:
        return {
            "id": reservation.id,
            "order_id": reservation.order_id,
            "order_item_id": reservation.order_item_id,
            "series_id": reservation.series_id,
            "quantity": reservation.quantity,
            "status": reservation.status,
            "expires_at": cls._aware(reservation.expires_at, field="expires_at").isoformat(),
            "allocated_at": (
                cls._aware(reservation.allocated_at, field="allocated_at").isoformat()
                if reservation.allocated_at is not None else None
            ),
            "released_at": (
                cls._aware(reservation.released_at, field="released_at").isoformat()
                if reservation.released_at is not None else None
            ),
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
