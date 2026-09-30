"""Server-calculated coupon pricing and lifecycle-safe redemption handling."""

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.audit import AuditService
from app.core.idempotency import IdempotencyService, canonical_payload, fingerprint
from app.core.money import require_paise
from app.core.permissions import RoleName
from app.core.transaction_locks import lock_key_for_transaction
from app.exceptions import AuthorizationError, ConflictError, InvariantViolationError, ValidationError
from app.models.coupon import Coupon, CouponDiscountType, CouponRedemption, CouponRedemptionStatus
from app.models.order import Order, OrderStatus
from app.models.user import IdempotencyRecord, User


_COUPON_CODE = re.compile(r"[A-Z0-9][A-Z0-9_-]{2,63}")
_ACTIVE_REDEMPTION_STATUSES = (
    CouponRedemptionStatus.RESERVED,
    CouponRedemptionStatus.CONSUMED,
)


@dataclass(slots=True)
class CouponMutationResult:
    coupon: Coupon
    response_payload: dict[str, object]
    replayed: bool


@dataclass(frozen=True, slots=True)
class CouponReservation:
    """The exact immutable price effect to write into a new order."""

    coupon: Coupon
    redemption: CouponRedemption
    discount_paise: int


class CouponService:
    """Owns coupon definition, reservation, consumption, and release rules."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.audit = AuditService(session)
        self.idempotency = IdempotencyService(session)

    async def create(
        self,
        *,
        actor_user_id: UUID,
        code: str,
        description: str | None,
        discount_type: CouponDiscountType,
        fixed_discount_paise: int | None,
        percentage_bps: int | None,
        max_discount_paise: int | None,
        minimum_order_paise: int,
        usage_limit: int | None,
        per_user_limit: int | None,
        starts_at: datetime | None,
        ends_at: datetime | None,
        idempotency_key: str,
        commit: bool,
    ) -> CouponMutationResult:
        """Create an immutable-for-redemption coupon policy under admin control."""

        await self._require_admin(actor_user_id)
        definition = self._validate_definition(
            code=code,
            description=description,
            discount_type=discount_type,
            fixed_discount_paise=fixed_discount_paise,
            percentage_bps=percentage_bps,
            max_discount_paise=max_discount_paise,
            minimum_order_paise=minimum_order_paise,
            usage_limit=usage_limit,
            per_user_limit=per_user_limit,
            starts_at=starts_at,
            ends_at=ends_at,
        )
        scope = f"admin:{actor_user_id}:coupon.create"
        request_fingerprint = fingerprint({"action": "create", **definition})
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay(replay)

        await lock_key_for_transaction(self.session, "coupon-code", str(definition["code"]))
        existing = await self.session.scalar(
            select(Coupon).where(Coupon.code == definition["code"]).with_for_update()
        )
        if existing is not None:
            raise ConflictError("COUPON_CODE_EXISTS", "A coupon with this code already exists")

        coupon = Coupon(
            id=uuid4(),
            code=str(definition["code"]),
            description=definition["description"],
            discount_type=definition["discount_type"],
            fixed_discount_paise=definition["fixed_discount_paise"],
            percentage_bps=definition["percentage_bps"],
            max_discount_paise=definition["max_discount_paise"],
            minimum_order_paise=int(definition["minimum_order_paise"]),
            usage_limit=definition["usage_limit"],
            per_user_limit=definition["per_user_limit"],
            starts_at=definition["starts_at"],
            ends_at=definition["ends_at"],
            is_active=True,
            created_by_user_id=actor_user_id,
        )
        self.session.add(coupon)
        record = self.idempotency.record(
            actor_scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint,
            resource_type="coupon",
            resource_id=coupon.id,
            status_code=201,
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="coupon",
            entity_id=coupon.id,
            action="COUPON_CREATED",
            before_state=None,
            after_state=self.coupon_state(coupon),
        )
        await self.session.flush()
        response = self.snapshot(coupon)
        record.response_payload = response
        await self._finish(commit=commit)
        return CouponMutationResult(coupon, response, False)

    async def deactivate(
        self,
        *,
        coupon_id: UUID,
        actor_user_id: UUID,
        reason: str,
        idempotency_key: str,
        commit: bool,
    ) -> CouponMutationResult:
        """Stop new reservations without rewriting existing price evidence."""

        await self._require_admin(actor_user_id)
        reason = self._text(reason, field="reason", maximum=500)
        coupon = await self._locked_coupon(coupon_id)
        scope = f"admin:{actor_user_id}:coupon.deactivate"
        request_fingerprint = fingerprint(
            {"action": "deactivate", "coupon_id": coupon_id, "reason": reason}
        )
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay(replay)

        before = self.coupon_state(coupon)
        coupon.is_active = False
        record = self.idempotency.record(
            actor_scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint,
            resource_type="coupon",
            resource_id=coupon.id,
        )
        if before["is_active"]:
            self.audit.record(
                actor_user_id=actor_user_id,
                entity_type="coupon",
                entity_id=coupon.id,
                action="COUPON_DEACTIVATED",
                before_state=before,
                after_state=self.coupon_state(coupon),
                reason=reason,
            )
        await self.session.flush()
        response = self.snapshot(coupon)
        record.response_payload = response
        await self._finish(commit=commit)
        return CouponMutationResult(coupon, response, False)

    async def list_for_admin(self, *, limit: int) -> list[Coupon]:
        """Return a bounded newest-first coupon list for the admin panel."""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 < limit <= 1_000:
            raise ValidationError("INVALID_LIMIT", "limit must be an integer between 1 and 1000")
        return list(
            await self.session.scalars(
                select(Coupon).order_by(Coupon.created_at.desc(), Coupon.id).limit(limit)
            )
        )

    async def reserve_for_order(
        self,
        *,
        order: Order,
        coupon_code: str,
        buyer_user_id: UUID,
        gross_paise: int,
    ) -> CouponReservation:
        """Reserve a coupon use and freeze its exact rule and price effect.

        The caller has already locked its catalog inventory and is creating a
        brand-new order in the same transaction. A coupon row lock serializes
        the global and per-user limit checks, including the empty-result case.
        """

        if order.id is None or not isinstance(order.id, UUID):
            raise ValidationError("INVALID_ORDER", "A coupon redemption requires a durable order identifier")
        if order.buyer_user_id != buyer_user_id:
            raise InvariantViolationError("Coupon redemption buyer does not match the order")
        gross = require_paise(gross_paise, field="gross_paise")
        code = self.normalize_code(coupon_code)
        await lock_key_for_transaction(self.session, "coupon-code", code)
        coupon = await self.session.scalar(
            select(Coupon).where(Coupon.code == code).with_for_update()
        )
        if coupon is None:
            raise ValidationError("UNKNOWN_COUPON", "Coupon code is not recognized")
        now = datetime.now(timezone.utc)
        self._assert_currently_eligible(coupon, gross_paise=gross, now=now)
        existing = await self.session.scalar(
            select(CouponRedemption)
            .where(CouponRedemption.order_id == order.id)
            .with_for_update()
        )
        if existing is not None:
            raise ConflictError("COUPON_REDEMPTION_EXISTS", "Order already has a coupon price snapshot")
        if coupon.usage_limit is not None and coupon.active_redemption_count >= coupon.usage_limit:
            raise ConflictError("COUPON_USAGE_LIMIT_REACHED", "Coupon usage limit has been reached")
        if coupon.per_user_limit is not None:
            user_count = await self.session.scalar(
                select(func.count(CouponRedemption.id)).where(
                    CouponRedemption.coupon_id == coupon.id,
                    CouponRedemption.buyer_user_id == buyer_user_id,
                    CouponRedemption.status.in_(_ACTIVE_REDEMPTION_STATUSES),
                )
            )
            if int(user_count or 0) >= coupon.per_user_limit:
                raise ConflictError("COUPON_USER_LIMIT_REACHED", "Coupon user limit has been reached")
        discount = self._discount_for(coupon, gross_paise=gross)
        if discount >= gross:
            raise ConflictError(
                "COUPON_ZERO_VALUE_ORDER_UNSUPPORTED",
                "Coupon cannot reduce this payment-required order to zero",
            )

        before_coupon = self.coupon_state(coupon)
        redemption = CouponRedemption(
            id=uuid4(),
            coupon_id=coupon.id,
            order_id=order.id,
            buyer_user_id=buyer_user_id,
            coupon_code_snapshot=coupon.code,
            rule_snapshot=self.rule_snapshot(coupon),
            gross_paise=gross,
            discount_paise=discount,
            net_paise=gross - discount,
            status=CouponRedemptionStatus.RESERVED,
            reserved_at=now,
        )
        self.session.add(redemption)
        coupon.active_redemption_count += 1
        self.audit.record(
            actor_user_id=buyer_user_id,
            entity_type="coupon",
            entity_id=coupon.id,
            action="COUPON_REDEMPTION_RESERVED",
            before_state=before_coupon,
            after_state=self.coupon_state(coupon),
        )
        self.audit.record(
            actor_user_id=buyer_user_id,
            entity_type="coupon_redemption",
            entity_id=redemption.id,
            action="COUPON_REDEMPTION_RESERVED",
            before_state=None,
            after_state=self.redemption_state(redemption),
        )
        return CouponReservation(coupon=coupon, redemption=redemption, discount_paise=discount)

    async def consume_for_settled_order(
        self, *, order: Order, actor_user_id: UUID | None
    ) -> CouponRedemption | None:
        """Mark a held redemption consumed only with the trusted order settlement."""

        if (
            order.status not in {OrderStatus.PAID, OrderStatus.FULFILLED}
            or order.settlement_reference_id is None
            or order.settled_at is None
        ):
            raise ConflictError(
                "ORDER_NOT_SETTLED", "Coupon redemption can be consumed only with a durable order settlement"
            )
        redemption = await self.session.scalar(
            select(CouponRedemption)
            .where(CouponRedemption.order_id == order.id)
            .with_for_update()
        )
        if redemption is None:
            return None
        self._assert_redemption_matches_order(redemption, order)
        if redemption.status == CouponRedemptionStatus.CONSUMED:
            return redemption
        if redemption.status != CouponRedemptionStatus.RESERVED:
            raise ConflictError(
                "COUPON_REDEMPTION_NOT_SETTLEABLE",
                "A released coupon redemption cannot be consumed",
            )
        before = self.redemption_state(redemption)
        redemption.status = CouponRedemptionStatus.CONSUMED
        redemption.consumed_at = datetime.now(timezone.utc)
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="coupon_redemption",
            entity_id=redemption.id,
            action="COUPON_REDEMPTION_CONSUMED",
            before_state=before,
            after_state=self.redemption_state(redemption),
        )
        return redemption

    async def release_for_cancelled_order(
        self, *, order: Order, actor_user_id: UUID | None, reason: str
    ) -> CouponRedemption | None:
        """Release only an unsettled reservation so the coupon may be reused."""

        if order.settlement_reference_id is not None or order.settled_at is not None:
            raise ConflictError(
                "COUPON_REDEMPTION_NOT_RELEASABLE",
                "A settlement-linked coupon redemption cannot be released",
            )
        redemption = await self.session.scalar(
            select(CouponRedemption)
            .where(CouponRedemption.order_id == order.id)
            .with_for_update()
        )
        if redemption is None:
            return None
        self._assert_redemption_matches_order(redemption, order)
        if redemption.status == CouponRedemptionStatus.RELEASED:
            return redemption
        if redemption.status != CouponRedemptionStatus.RESERVED:
            raise ConflictError(
                "COUPON_REDEMPTION_NOT_RELEASABLE",
                "A consumed coupon redemption cannot be released",
            )
        coupon = await self.session.scalar(
            select(Coupon).where(Coupon.id == redemption.coupon_id).with_for_update()
        )
        if coupon is None or coupon.active_redemption_count <= 0:
            raise InvariantViolationError("Coupon redemption count is inconsistent")
        before_coupon = self.coupon_state(coupon)
        before_redemption = self.redemption_state(redemption)
        coupon.active_redemption_count -= 1
        redemption.status = CouponRedemptionStatus.RELEASED
        redemption.released_at = datetime.now(timezone.utc)
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="coupon",
            entity_id=coupon.id,
            action="COUPON_REDEMPTION_RELEASED",
            before_state=before_coupon,
            after_state=self.coupon_state(coupon),
            reason=reason,
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="coupon_redemption",
            entity_id=redemption.id,
            action="COUPON_REDEMPTION_RELEASED",
            before_state=before_redemption,
            after_state=self.redemption_state(redemption),
            reason=reason,
        )
        return redemption

    async def _locked_coupon(self, coupon_id: UUID) -> Coupon:
        coupon = await self.session.scalar(
            select(Coupon).where(Coupon.id == coupon_id).with_for_update()
        )
        if coupon is None:
            raise ValidationError("UNKNOWN_COUPON", "Coupon does not exist")
        return coupon

    async def _require_admin(self, actor_user_id: UUID) -> User:
        user = await self.session.scalar(
            select(User)
            .options(selectinload(User.roles))
            .where(User.id == actor_user_id)
            .with_for_update()
        )
        if user is None or not user.is_active:
            raise AuthorizationError("An active administrator is required")
        if RoleName.ADMIN.value not in {role.name for role in user.roles}:
            raise AuthorizationError("Administrator role is required")
        return user

    async def _replay(self, record: IdempotencyRecord) -> CouponMutationResult:
        if record.resource_id is None or not isinstance(record.response_payload, dict):
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior coupon mutation cannot be recovered")
        coupon = await self.session.get(Coupon, record.resource_id)
        if coupon is None:
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior coupon no longer exists")
        return CouponMutationResult(coupon, dict(record.response_payload), True)

    @classmethod
    def _validate_definition(
        cls,
        *,
        code: object,
        description: object,
        discount_type: object,
        fixed_discount_paise: object,
        percentage_bps: object,
        max_discount_paise: object,
        minimum_order_paise: object,
        usage_limit: object,
        per_user_limit: object,
        starts_at: object,
        ends_at: object,
    ) -> dict[str, object]:
        normalized_code = cls.normalize_code(code)
        normalized_description = cls._optional_text(description, field="description", maximum=500)
        if not isinstance(discount_type, CouponDiscountType):
            raise ValidationError("INVALID_COUPON", "discount_type must be FIXED_PAISE or PERCENT_BPS")
        normalized_minimum = cls._nonnegative_paise(
            minimum_order_paise, field="minimum_order_paise"
        )
        normalized_usage = cls._optional_positive_int(usage_limit, field="usage_limit")
        normalized_per_user = cls._optional_positive_int(per_user_limit, field="per_user_limit")
        normalized_start = cls._optional_aware_datetime(starts_at, field="starts_at")
        normalized_end = cls._optional_aware_datetime(ends_at, field="ends_at")
        if normalized_start is not None and normalized_end is not None and normalized_end <= normalized_start:
            raise ValidationError("INVALID_COUPON_WINDOW", "ends_at must be after starts_at")
        if discount_type == CouponDiscountType.FIXED_PAISE:
            if percentage_bps is not None or max_discount_paise is not None:
                raise ValidationError(
                    "INVALID_COUPON", "Fixed coupons cannot specify percentage_bps or max_discount_paise"
                )
            return {
                "code": normalized_code,
                "description": normalized_description,
                "discount_type": discount_type,
                "fixed_discount_paise": require_paise(
                    fixed_discount_paise, field="fixed_discount_paise"
                ),
                "percentage_bps": None,
                "max_discount_paise": None,
                "minimum_order_paise": normalized_minimum,
                "usage_limit": normalized_usage,
                "per_user_limit": normalized_per_user,
                "starts_at": normalized_start,
                "ends_at": normalized_end,
            }
        if fixed_discount_paise is not None:
            raise ValidationError("INVALID_COUPON", "Percentage coupons cannot specify fixed_discount_paise")
        if isinstance(percentage_bps, bool) or not isinstance(percentage_bps, int) or not 0 < percentage_bps <= 10_000:
            raise ValidationError("INVALID_COUPON", "percentage_bps must be an integer between 1 and 10000")
        return {
            "code": normalized_code,
            "description": normalized_description,
            "discount_type": discount_type,
            "fixed_discount_paise": None,
            "percentage_bps": percentage_bps,
            "max_discount_paise": (
                require_paise(max_discount_paise, field="max_discount_paise")
                if max_discount_paise is not None
                else None
            ),
            "minimum_order_paise": normalized_minimum,
            "usage_limit": normalized_usage,
            "per_user_limit": normalized_per_user,
            "starts_at": normalized_start,
            "ends_at": normalized_end,
        }

    @staticmethod
    def normalize_code(value: object) -> str:
        if not isinstance(value, str):
            raise ValidationError("INVALID_COUPON_CODE", "coupon_code must be a string")
        code = value.strip().upper()
        if not _COUPON_CODE.fullmatch(code):
            raise ValidationError(
                "INVALID_COUPON_CODE",
                "coupon_code must be 3-64 uppercase letters, digits, hyphens, or underscores",
            )
        return code

    @classmethod
    def _assert_currently_eligible(
        cls, coupon: Coupon, *, gross_paise: int, now: datetime
    ) -> None:
        if not coupon.is_active:
            raise ConflictError("COUPON_INACTIVE", "Coupon is not active")
        if coupon.starts_at is not None and cls._as_utc(coupon.starts_at) > now:
            raise ConflictError("COUPON_NOT_STARTED", "Coupon is not active yet")
        if coupon.ends_at is not None and cls._as_utc(coupon.ends_at) <= now:
            raise ConflictError("COUPON_EXPIRED", "Coupon has expired")
        if gross_paise < coupon.minimum_order_paise:
            raise ConflictError("COUPON_MINIMUM_NOT_MET", "Order does not meet the coupon minimum")

    @staticmethod
    def _discount_for(coupon: Coupon, *, gross_paise: int) -> int:
        if coupon.discount_type == CouponDiscountType.FIXED_PAISE:
            if coupon.fixed_discount_paise is None:
                raise InvariantViolationError("Coupon fixed-discount configuration is invalid")
            return coupon.fixed_discount_paise
        if coupon.discount_type == CouponDiscountType.PERCENT_BPS:
            if coupon.percentage_bps is None:
                raise InvariantViolationError("Coupon percentage configuration is invalid")
            discount = gross_paise * coupon.percentage_bps // 10_000
            if coupon.max_discount_paise is not None:
                discount = min(discount, coupon.max_discount_paise)
            if discount <= 0:
                raise ConflictError("COUPON_DISCOUNT_ZERO", "Coupon does not discount this order by a whole paise")
            return discount
        raise InvariantViolationError("Coupon discount type is invalid")

    @classmethod
    def _assert_redemption_matches_order(cls, redemption: CouponRedemption, order: Order) -> None:
        if (
            redemption.buyer_user_id != order.buyer_user_id
            or redemption.gross_paise != order.subtotal_paise
            or redemption.discount_paise != order.discount_paise
            or redemption.net_paise != order.total_paise
            or redemption.coupon_code_snapshot != order.coupon_code_snapshot
        ):
            raise InvariantViolationError("Coupon redemption does not match the frozen order price")

    @staticmethod
    def _text(value: object, *, field: str, maximum: int) -> str:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
            raise ValidationError("INVALID_COUPON", f"{field} must be a non-empty string up to {maximum} characters")
        return value.strip()

    @staticmethod
    def _optional_text(value: object, *, field: str, maximum: int) -> str | None:
        if value is None:
            return None
        return CouponService._text(value, field=field, maximum=maximum)

    @staticmethod
    def _nonnegative_paise(value: object, *, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValidationError("INVALID_COUPON", f"{field} must be a non-negative integer paise value")
        return value

    @staticmethod
    def _optional_positive_int(value: object, *, field: str) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 2_000_000_000:
            raise ValidationError("INVALID_COUPON", f"{field} must be a positive integer")
        return value

    @staticmethod
    def _optional_aware_datetime(value: object, *, field: str) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValidationError("INVALID_COUPON_WINDOW", f"{field} must include a timezone")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    @staticmethod
    def rule_snapshot(coupon: Coupon) -> dict[str, object]:
        return {
            "code": coupon.code,
            "discount_type": coupon.discount_type,
            "fixed_discount_paise": coupon.fixed_discount_paise,
            "percentage_bps": coupon.percentage_bps,
            "max_discount_paise": coupon.max_discount_paise,
            "minimum_order_paise": coupon.minimum_order_paise,
            "usage_limit": coupon.usage_limit,
            "per_user_limit": coupon.per_user_limit,
            "starts_at": CouponService._datetime(coupon.starts_at),
            "ends_at": CouponService._datetime(coupon.ends_at),
        }

    @staticmethod
    def coupon_state(coupon: Coupon) -> dict[str, object]:
        return {
            "id": coupon.id,
            "code": coupon.code,
            "description": coupon.description,
            "discount_type": coupon.discount_type,
            "fixed_discount_paise": coupon.fixed_discount_paise,
            "percentage_bps": coupon.percentage_bps,
            "max_discount_paise": coupon.max_discount_paise,
            "minimum_order_paise": coupon.minimum_order_paise,
            "usage_limit": coupon.usage_limit,
            "per_user_limit": coupon.per_user_limit,
            "active_redemption_count": coupon.active_redemption_count,
            "starts_at": CouponService._datetime(coupon.starts_at),
            "ends_at": CouponService._datetime(coupon.ends_at),
            "is_active": coupon.is_active,
            "created_by_user_id": coupon.created_by_user_id,
        }

    @staticmethod
    def redemption_state(redemption: CouponRedemption) -> dict[str, object]:
        return {
            "id": redemption.id,
            "coupon_id": redemption.coupon_id,
            "order_id": redemption.order_id,
            "buyer_user_id": redemption.buyer_user_id,
            "coupon_code_snapshot": redemption.coupon_code_snapshot,
            "rule_snapshot": redemption.rule_snapshot,
            "gross_paise": redemption.gross_paise,
            "discount_paise": redemption.discount_paise,
            "net_paise": redemption.net_paise,
            "status": redemption.status,
            "reserved_at": CouponService._datetime(redemption.reserved_at),
            "consumed_at": CouponService._datetime(redemption.consumed_at),
            "released_at": CouponService._datetime(redemption.released_at),
        }

    @classmethod
    def snapshot(cls, coupon: Coupon) -> dict[str, object]:
        return canonical_payload(cls.coupon_state(coupon))

    @staticmethod
    def _datetime(value: datetime | None) -> str | None:
        if value is None:
            return None
        return CouponService._as_utc(value).isoformat()

    async def _finish(self, *, commit: bool) -> None:
        await self.session.flush()
        if commit:
            await self.session.commit()
