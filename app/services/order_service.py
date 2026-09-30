"""Order creation, short-lived reservations, and safe cancellation."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.core.audit import AuditService
from app.core.idempotency import IdempotencyService, canonical_payload, fingerprint
from app.core.money import require_inr_currency
from app.core.permissions import RoleName
from app.core.state_machine import ORDER_TRANSITIONS, require_transition
from app.exceptions import AuthorizationError, ConflictError, ValidationError
from app.models.order import (
    DeliveryStatus,
    Order,
    OrderItem,
    OrderStatus,
    TicketProductType,
    TicketReservation,
)
from app.models.payment import PaymentAttempt
from app.models.p2p_match import P2PMatch
from app.models.ticket_package import TicketPackage
from app.models.ticket_series import TicketSeries
from app.models.user import IdempotencyRecord, User
from app.repositories.order_repository import get_order, get_order_for_buyer, list_orders_for_buyer
from app.repositories.ticket_repository import get_ticket_package, get_ticket_series
from app.services.coupon_service import CouponService
from app.services.inventory_service import InventoryService, ReservationPlan


@dataclass(frozen=True, slots=True)
class OrderSelection:
    product_type: TicketProductType
    product_id: UUID
    quantity: int
    coupon_code: str | None = None


@dataclass(slots=True)
class OrderMutationResult:
    order: Order
    response_payload: dict[str, object]
    replayed: bool


class OrderService:
    """Server-side pricing and inventory reservation for one catalog product per order."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.audit = AuditService(session)
        self.idempotency = IdempotencyService(session)
        self.inventory = InventoryService(session)

    async def create(
        self,
        *,
        buyer_user_id: UUID,
        selection: OrderSelection,
        idempotency_key: str,
        commit: bool,
    ) -> OrderMutationResult:
        """Create a PENDING_PAYMENT order using catalog price, never client price."""

        selection = self._validate_selection(selection)
        await self._locked_active_user(buyer_user_id)
        scope = f"user:{buyer_user_id}:order.create"
        request_fingerprint = fingerprint(
            {
                "product_type": selection.product_type,
                "product_id": selection.product_id,
                "quantity": selection.quantity,
                "coupon_code": selection.coupon_code,
            }
        )
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay(replay)
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(minutes=get_settings().order_reservation_minutes)
        package: TicketPackage | None = None
        if selection.product_type == TicketProductType.SERIES:
            series = await get_ticket_series(self.session, selection.product_id, for_update=True)
            if series is None:
                raise ValidationError("UNKNOWN_TICKET_SERIES", "Ticket series does not exist")
            product_name = series.name
            unit_price = series.price_paise
            plans = [ReservationPlan(series_id=series.id, quantity=selection.quantity)]
            contents_snapshot: dict[str, object] | None = None
        else:
            package = await get_ticket_package(self.session, selection.product_id, for_update=True)
            if package is None:
                raise ValidationError("UNKNOWN_TICKET_PACKAGE", "Ticket package does not exist")
            if not package.is_active:
                raise ConflictError("TICKET_PACKAGE_INACTIVE", "Ticket package is not available for sale")
            if package.inventory_limit is not None:
                remaining = package.inventory_limit - package.sold_count - package.reserved_count
                if remaining < selection.quantity:
                    raise ConflictError("INSUFFICIENT_PACKAGE_INVENTORY", "Ticket package inventory is exhausted")
            if not package.items:
                raise ConflictError("INVALID_TICKET_PACKAGE", "Ticket package has no included series")
            product_name = package.name
            unit_price = package.price_paise
            plans = [
                ReservationPlan(series_id=item.series_id, quantity=item.quantity * selection.quantity)
                for item in package.items
            ]
            contents_snapshot = {
                "items": [
                    {"series_id": str(item.series_id), "quantity_per_package": item.quantity}
                    for item in sorted(package.items, key=lambda value: str(value.series_id))
                ]
            }
        line_total = unit_price * selection.quantity
        order = Order(
            id=uuid4(), buyer_user_id=buyer_user_id, status=OrderStatus.PENDING_PAYMENT,
            delivery_status=DeliveryStatus.NOT_STARTED, subtotal_paise=line_total, discount_paise=0,
            total_paise=line_total, currency="INR", coupon_code_snapshot=None, expires_at=expires_at,
        )
        order_item = OrderItem(
            id=uuid4(), order_id=order.id, order=order, product_type=selection.product_type,
            product_id=selection.product_id, product_name_snapshot=product_name, unit_price_paise=unit_price,
            quantity=selection.quantity, line_total_paise=line_total, currency="INR",
            package_contents_snapshot=contents_snapshot,
        )
        self.session.add_all((order, order_item))
        if selection.coupon_code is not None:
            coupon_reservation = await CouponService(self.session).reserve_for_order(
                order=order,
                coupon_code=selection.coupon_code,
                buyer_user_id=buyer_user_id,
                gross_paise=line_total,
            )
            order.discount_paise = coupon_reservation.discount_paise
            order.total_paise = line_total - coupon_reservation.discount_paise
            order.coupon_code_snapshot = coupon_reservation.coupon.code
        reservations = await self.inventory.reserve(
            order=order, order_item=order_item, plans=plans, expires_at=expires_at, actor_user_id=buyer_user_id
        )
        if package is not None:
            before_package = self._package_state(package)
            package.reserved_count += selection.quantity
            self.audit.record(
                actor_user_id=buyer_user_id, entity_type="ticket_package", entity_id=package.id,
                action="TICKET_PACKAGE_RESERVED", before_state=before_package,
                after_state=self._package_state(package),
            )
        record = self.idempotency.record(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint,
            resource_type="order", resource_id=order.id, status_code=201,
        )
        self.audit.record(
            actor_user_id=buyer_user_id, entity_type="order", entity_id=order.id,
            action="ORDER_CREATED", before_state=None,
            after_state=self._order_state(order, items=[order_item], reservations=reservations),
        )
        self.audit.record(
            actor_user_id=buyer_user_id, entity_type="order_item", entity_id=order_item.id,
            action="ORDER_ITEM_PRICE_SNAPSHOTTED", before_state=None, after_state=self._order_item_state(order_item),
        )
        await self.session.flush()
        response = self.snapshot(order, items=[order_item], reservations=reservations)
        record.response_payload = response
        await self._finish(commit=commit)
        return OrderMutationResult(order, response, False)

    async def cancel(
        self, *, order_id: UUID, actor_user_id: UUID, idempotency_key: str, commit: bool
    ) -> OrderMutationResult:
        """Cancel only an unexposed unsettled order and release its active claims."""

        actor = await self._active_user(actor_user_id)
        order = await self._locked_order(order_id)
        self._assert_order_actor(order, actor)
        scope = f"user:{actor_user_id}:order.cancel"
        request_fingerprint = fingerprint({"action": "cancel", "order_id": order_id})
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay(replay)
        return await self._cancel_locked(
            order=order,
            actor_user_id=actor_user_id,
            scope=scope,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            action="ORDER_CANCELLED",
            reason="Cancelled by authorized user",
            commit=commit,
        )

    async def expire_pending(
        self, *, order_id: UUID, idempotency_key: str, commit: bool
    ) -> OrderMutationResult:
        """Internal scheduler entry point; it is intentionally not exposed as an API route."""

        order = await self._locked_order(order_id)
        scope = "system:order.expire"
        request_fingerprint = fingerprint({"action": "expire", "order_id": order_id})
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay(replay)
        now = datetime.now(timezone.utc)
        if now < self._utc(order.expires_at):
            raise ConflictError("ORDER_NOT_EXPIRED", "Order reservation has not expired")
        return await self._cancel_locked(
            order=order,
            actor_user_id=None,
            scope=scope,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            action="ORDER_EXPIRED",
            reason="Reservation expired before verified settlement",
            commit=commit,
        )

    async def get_for_actor(self, *, order_id: UUID, actor_user_id: UUID) -> Order:
        actor = await self._active_user(actor_user_id)
        if RoleName.ADMIN.value in {role.name for role in actor.roles}:
            order = await get_order(self.session, order_id)
        else:
            order = await get_order_for_buyer(self.session, order_id=order_id, buyer_user_id=actor_user_id)
        if order is None:
            raise ValidationError("UNKNOWN_ORDER", "Order does not exist")
        return order

    async def checkout_state_for_buyer(self, *, order_id: UUID, buyer_user_id: UUID) -> dict[str, object]:
        """Expose only an owner's latest payment navigation IDs, never a payment-success assertion."""

        await self._active_user(buyer_user_id)
        order = await get_order_for_buyer(self.session, order_id=order_id, buyer_user_id=buyer_user_id)
        if order is None:
            raise ValidationError("UNKNOWN_ORDER", "Order does not exist")
        attempt_id = await self.session.scalar(
            select(PaymentAttempt.id)
            .where(PaymentAttempt.order_id == order_id, PaymentAttempt.buyer_user_id == buyer_user_id)
            .order_by(PaymentAttempt.created_at.desc(), PaymentAttempt.id.desc())
            .limit(1)
        )
        match_id = await self.session.scalar(
            select(P2PMatch.id)
            .where(P2PMatch.order_id == order_id, P2PMatch.buyer_user_id == buyer_user_id)
            .order_by(P2PMatch.created_at.desc(), P2PMatch.id.desc())
            .limit(1)
        )
        return {
            "order_id": order.id, "order_status": order.status,
            "payment_attempt_id": attempt_id, "p2p_match_id": match_id,
        }

    async def list_for_buyer(self, *, buyer_user_id: UUID) -> list[Order]:
        await self._active_user(buyer_user_id)
        return await list_orders_for_buyer(self.session, buyer_user_id)

    async def _cancel_locked(
        self,
        *,
        order: Order,
        actor_user_id: UUID | None,
        scope: str,
        idempotency_key: str,
        request_fingerprint: str,
        action: str,
        reason: str,
        commit: bool,
        allow_reconciled_p2p: bool = False,
    ) -> OrderMutationResult:
        cancellable_statuses = {OrderStatus.PENDING_PAYMENT, OrderStatus.WAITING_FOR_MATCH}
        if allow_reconciled_p2p:
            cancellable_statuses.add(OrderStatus.AWAITING_PAYMENT)
        if order.status not in cancellable_statuses or order.settlement_reference_id is not None:
            raise ConflictError(
                "ORDER_CANNOT_CANCEL",
                "Only an unsettled order without exposed payment instructions can be cancelled",
            )
        if len(order.items) != 1:
            raise ConflictError("ORDER_INCONSISTENT", "Phase 3 orders must have exactly one order item")
        item = order.items[0]
        package = await self._package_for_item(item)
        before_order = self.snapshot(order)
        before_package = self._package_state(package) if package is not None else None
        if package is not None:
            if package.reserved_count < item.quantity:
                raise ConflictError("INVENTORY_INCONSISTENT", "Package reservation counter is inconsistent")
            package.reserved_count -= item.quantity
        await CouponService(self.session).release_for_cancelled_order(
            order=order, actor_user_id=actor_user_id, reason=reason
        )
        await self.inventory.release(order=order, actor_user_id=actor_user_id, reason=reason)
        require_transition(
            current=order.status, target=OrderStatus.CANCELLED, transitions=ORDER_TRANSITIONS, resource="Order"
        )
        order.status = OrderStatus.CANCELLED
        record = self.idempotency.record(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint,
            resource_type="order", resource_id=order.id,
        )
        self.audit.record(
            actor_user_id=actor_user_id, entity_type="order", entity_id=order.id, action=action,
            before_state=before_order, after_state=self._order_state(order), reason=reason,
        )
        if package is not None and before_package is not None:
            self.audit.record(
                actor_user_id=actor_user_id, entity_type="ticket_package", entity_id=package.id,
                action="TICKET_PACKAGE_RESERVATION_RELEASED", before_state=before_package,
                after_state=self._package_state(package), reason=reason,
            )
        await self.session.flush()
        response = self.snapshot(order)
        record.response_payload = response
        await self._finish(commit=commit)
        return OrderMutationResult(order, response, False)

    async def _package_for_item(self, item: OrderItem) -> TicketPackage | None:
        if item.product_type == TicketProductType.SERIES:
            return None
        if item.product_type != TicketProductType.PACKAGE:
            raise ConflictError("ORDER_INCONSISTENT", "Order item has an unknown product type")
        package = await get_ticket_package(self.session, item.product_id, for_update=True)
        if package is None:
            raise ConflictError("ORDER_INCONSISTENT", "Package backing this order no longer exists")
        return package

    async def _locked_order(self, order_id: UUID) -> Order:
        order = await get_order(self.session, order_id, for_update=True)
        if order is None:
            raise ValidationError("UNKNOWN_ORDER", "Order does not exist")
        return order

    async def _replay(self, record: IdempotencyRecord) -> OrderMutationResult:
        if record.resource_id is None:
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior order mutation has no resource")
        order = await get_order(self.session, record.resource_id)
        if order is None or not isinstance(record.response_payload, dict):
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior order mutation cannot be recovered")
        return OrderMutationResult(order, dict(record.response_payload), True)

    async def _locked_active_user(self, user_id: UUID) -> User:
        user = await self.session.scalar(
            select(User).options(selectinload(User.roles)).where(User.id == user_id).with_for_update()
        )
        if user is None or not user.is_active:
            raise AuthorizationError("An active user account is required to create an order")
        return user

    async def _active_user(self, user_id: UUID) -> User:
        user = await self.session.scalar(
            select(User).options(selectinload(User.roles)).where(User.id == user_id)
        )
        if user is None or not user.is_active:
            raise AuthorizationError("An active user account is required")
        return user

    @staticmethod
    def _assert_order_actor(order: Order, actor: User) -> None:
        if actor.id == order.buyer_user_id or RoleName.ADMIN.value in {role.name for role in actor.roles}:
            return
        raise AuthorizationError("You cannot change this order")

    async def _finish(self, *, commit: bool) -> None:
        await self.session.flush()
        if commit:
            await self.session.commit()

    @staticmethod
    def _validate_selection(value: object) -> OrderSelection:
        if not isinstance(value, OrderSelection):
            raise ValidationError("INVALID_ORDER", "Order selection has an invalid shape")
        if not isinstance(value.product_type, TicketProductType):
            raise ValidationError("INVALID_ORDER", "Product type must be SERIES or PACKAGE")
        if not isinstance(value.product_id, UUID):
            raise ValidationError("INVALID_ORDER", "Product ID must be a UUID")
        if isinstance(value.quantity, bool) or not isinstance(value.quantity, int) or not 0 < value.quantity <= 10_000:
            raise ValidationError("INVALID_ORDER", "Quantity must be an integer between 1 and 10000")
        coupon_code = (
            CouponService.normalize_code(value.coupon_code) if value.coupon_code is not None else None
        )
        return OrderSelection(
            product_type=value.product_type,
            product_id=value.product_id,
            quantity=value.quantity,
            coupon_code=coupon_code,
        )

    @classmethod
    def _order_state(
        cls,
        order: Order,
        *,
        items: list[OrderItem] | None = None,
        reservations: list[TicketReservation] | None = None,
    ) -> dict[str, object]:
        item_values = list(order.items) if items is None else items
        reservation_values = list(order.reservations) if reservations is None else reservations
        return {
            "id": order.id,
            "buyer_user_id": order.buyer_user_id,
            "status": order.status,
            "delivery_status": order.delivery_status,
            "subtotal_paise": order.subtotal_paise,
            "discount_paise": order.discount_paise,
            "total_paise": order.total_paise,
            "currency": require_inr_currency(order.currency),
            "coupon_code_snapshot": order.coupon_code_snapshot,
            "expires_at": cls._utc(order.expires_at).isoformat(),
            "settlement_reference_id": order.settlement_reference_id,
            "settled_at": cls._utc(order.settled_at).isoformat() if order.settled_at else None,
            "items": [cls._order_item_state(item) for item in item_values],
            "reservations": [cls._reservation_state(reservation) for reservation in reservation_values],
        }

    @staticmethod
    def _order_item_state(item: OrderItem) -> dict[str, object]:
        return {
            "id": item.id,
            "order_id": item.order_id,
            "product_type": item.product_type,
            "product_id": item.product_id,
            "product_name_snapshot": item.product_name_snapshot,
            "unit_price_paise": item.unit_price_paise,
            "quantity": item.quantity,
            "line_total_paise": item.line_total_paise,
            "currency": require_inr_currency(item.currency),
            "package_contents_snapshot": item.package_contents_snapshot,
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
            "expires_at": cls._utc(reservation.expires_at).isoformat(),
            "allocated_at": cls._utc(reservation.allocated_at).isoformat() if reservation.allocated_at else None,
            "released_at": cls._utc(reservation.released_at).isoformat() if reservation.released_at else None,
        }

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
    def _utc(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    @classmethod
    def snapshot(
        cls,
        order: Order,
        *,
        items: list[OrderItem] | None = None,
        reservations: list[TicketReservation] | None = None,
    ) -> dict[str, object]:
        return canonical_payload(cls._order_state(order, items=items, reservations=reservations))
