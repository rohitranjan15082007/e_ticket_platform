"""Evidence-backed affiliate attribution and pending commissions."""

from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.audit import AuditService
from app.core.idempotency import IdempotencyService, canonical_payload, fingerprint
from app.core.money import require_paise
from app.core.permissions import RoleName
from app.core.transaction_locks import lock_key_for_transaction
from app.exceptions import AuthorizationError, ConflictError, ValidationError
from app.models.affiliate import (
    Affiliate, AffiliateCommission, AffiliateCommissionStatus, AffiliateCommissionType,
    AffiliateConversion, AffiliateConversionStatus, AffiliateStatus,
)
from app.models.order import Order, OrderStatus
from app.models.user import User


class AffiliateService:
    """Manual conversion review records cannot credit a wallet."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.audit = AuditService(session)
        self.idempotency = IdempotencyService(session)

    async def create(
        self, *, actor_user_id: UUID, code: str, display_name: str,
        owner_user_id: UUID | None, commission_type: AffiliateCommissionType,
        fixed_commission_paise: int | None, percentage_bps: int | None,
        max_commission_paise: int | None, minimum_order_paise: int,
        idempotency_key: str, commit: bool,
    ) -> tuple[Affiliate, dict[str, object], bool]:
        await self._require_admin(actor_user_id)
        code = self._code(code)
        display_name = self._text(display_name, 120)
        minimum_order_paise = require_paise(minimum_order_paise, field="minimum_order_paise")
        try:
            commission_type = AffiliateCommissionType(commission_type)
        except (TypeError, ValueError) as error:
            raise ValidationError("INVALID_AFFILIATE_TYPE", "Unknown commission type") from error
        if commission_type == AffiliateCommissionType.FIXED_PAISE:
            fixed_commission_paise = require_paise(fixed_commission_paise, field="fixed_commission_paise")
            if percentage_bps is not None or max_commission_paise is not None:
                raise ValidationError("INVALID_AFFILIATE_RULE", "Fixed commission cannot have percentage fields")
        elif (
            fixed_commission_paise is not None or isinstance(percentage_bps, bool)
            or not isinstance(percentage_bps, int) or not 0 < percentage_bps <= 10_000
        ):
            raise ValidationError("INVALID_AFFILIATE_RULE", "Percentage commission requires valid basis points")
        if max_commission_paise is not None:
            max_commission_paise = require_paise(max_commission_paise, field="max_commission_paise")
        if owner_user_id is not None:
            owner = await self.session.get(User, owner_user_id)
            if owner is None or not owner.is_active:
                raise ValidationError("UNKNOWN_AFFILIATE_OWNER", "Owner must be an active user")
        values = dict(
            code=code, display_name=display_name, owner_user_id=owner_user_id,
            commission_type=commission_type, fixed_commission_paise=fixed_commission_paise,
            percentage_bps=percentage_bps, max_commission_paise=max_commission_paise,
            minimum_order_paise=minimum_order_paise,
        )
        scope = f"admin:{actor_user_id}:affiliate.create"
        digest = fingerprint({"action": "create", **values})
        replay = await self.idempotency.get_replay(actor_scope=scope, key=idempotency_key, request_fingerprint=digest)
        if replay is not None:
            affiliate = await self.session.get(Affiliate, replay.resource_id)
            if affiliate is None or not isinstance(replay.response_payload, dict):
                raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior affiliate mutation cannot be recovered")
            return affiliate, dict(replay.response_payload), True
        if owner_user_id is not None:
            await lock_key_for_transaction(self.session, "affiliate-owner", owner_user_id)
            if await self.session.scalar(select(Affiliate.id).where(Affiliate.owner_user_id == owner_user_id)):
                raise ConflictError("AFFILIATE_OWNER_EXISTS", "Owner already has an affiliate account")
        await lock_key_for_transaction(self.session, "affiliate-code", code)
        if await self.session.scalar(select(Affiliate.id).where(Affiliate.code == code)):
            raise ConflictError("AFFILIATE_CODE_EXISTS", "Affiliate code already exists")
        affiliate = Affiliate(id=uuid4(), **values, status=AffiliateStatus.PENDING, created_by_user_id=actor_user_id)
        self.session.add(affiliate)
        record = self.idempotency.record(
            actor_scope=scope, key=idempotency_key, request_fingerprint=digest,
            resource_type="affiliate", resource_id=affiliate.id, status_code=201,
        )
        self.audit.record(
            actor_user_id=actor_user_id, entity_type="affiliate", entity_id=affiliate.id,
            action="AFFILIATE_CREATED", before_state=None, after_state=self.snapshot(affiliate),
        )
        await self.session.flush()
        payload = self.snapshot(affiliate)
        record.response_payload = payload
        if commit:
            await self.session.commit()
        return affiliate, payload, False

    async def activate(
        self, *, affiliate_id: UUID, actor_user_id: UUID, idempotency_key: str, commit: bool
    ) -> tuple[Affiliate, dict[str, object], bool]:
        await self._require_admin(actor_user_id)
        scope = f"admin:{actor_user_id}:affiliate.activate"
        digest = fingerprint({"action": "activate", "affiliate_id": affiliate_id})
        replay = await self.idempotency.get_replay(actor_scope=scope, key=idempotency_key, request_fingerprint=digest)
        if replay is not None:
            affiliate = await self.session.get(Affiliate, replay.resource_id)
            if affiliate is None or not isinstance(replay.response_payload, dict):
                raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior affiliate mutation cannot be recovered")
            return affiliate, dict(replay.response_payload), True
        affiliate = await self.session.scalar(select(Affiliate).where(Affiliate.id == affiliate_id).with_for_update())
        if affiliate is None:
            raise ValidationError("UNKNOWN_AFFILIATE", "Affiliate does not exist")
        if affiliate.status == AffiliateStatus.SUSPENDED:
            raise ConflictError("AFFILIATE_SUSPENDED", "Suspended affiliate cannot be activated")
        before = self.snapshot(affiliate)
        affiliate.status = AffiliateStatus.ACTIVE
        affiliate.activated_by_user_id = actor_user_id
        affiliate.activated_at = datetime.now(timezone.utc)
        record = self.idempotency.record(
            actor_scope=scope, key=idempotency_key, request_fingerprint=digest,
            resource_type="affiliate", resource_id=affiliate.id,
        )
        self.audit.record(
            actor_user_id=actor_user_id, entity_type="affiliate", entity_id=affiliate.id,
            action="AFFILIATE_ACTIVATED", before_state=before, after_state=self.snapshot(affiliate),
        )
        await self.session.flush()
        payload = self.snapshot(affiliate)
        record.response_payload = payload
        if commit:
            await self.session.commit()
        return affiliate, payload, False

    async def attribute_settled_order(
        self, *, affiliate_id: UUID, order_id: UUID, evidence_reference: str,
        actor_user_id: UUID, idempotency_key: str, commit: bool,
    ) -> tuple[AffiliateConversion, AffiliateCommission, dict[str, object], bool]:
        """One admin-verified attribution per settled order; no cash movement."""

        await self._require_admin(actor_user_id)
        evidence_reference = self._text(evidence_reference, 255)
        scope = f"admin:{actor_user_id}:affiliate.attribute"
        digest = fingerprint({
            "action": "attribute", "affiliate_id": affiliate_id, "order_id": order_id,
            "evidence_reference": evidence_reference,
        })
        replay = await self.idempotency.get_replay(actor_scope=scope, key=idempotency_key, request_fingerprint=digest)
        if replay is not None:
            conversion = await self.session.get(AffiliateConversion, replay.resource_id)
            commission = await self.session.scalar(
                select(AffiliateCommission).where(AffiliateCommission.conversion_id == replay.resource_id)
            )
            if conversion is None or commission is None or not isinstance(replay.response_payload, dict):
                raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior conversion cannot be recovered")
            return conversion, commission, dict(replay.response_payload), True
        await lock_key_for_transaction(self.session, "affiliate-conversion", order_id)
        order = await self.session.scalar(select(Order).where(Order.id == order_id).with_for_update())
        affiliate = await self.session.scalar(select(Affiliate).where(Affiliate.id == affiliate_id).with_for_update())
        if order is None or affiliate is None:
            raise ValidationError("UNKNOWN_AFFILIATE_ORDER", "Order or affiliate does not exist")
        if (
            order.status not in {OrderStatus.PAID, OrderStatus.FULFILLED}
            or order.settlement_reference_id is None or order.settled_at is None
        ):
            raise ConflictError("ORDER_NOT_SETTLED", "Affiliate attribution requires a durable settlement")
        if affiliate.status != AffiliateStatus.ACTIVE:
            raise ConflictError("AFFILIATE_INACTIVE", "Affiliate is not active")
        if affiliate.owner_user_id == order.buyer_user_id:
            raise ConflictError("SELF_AFFILIATE_FORBIDDEN", "Affiliate owner cannot refer their own order")
        if order.total_paise < affiliate.minimum_order_paise:
            raise ConflictError("AFFILIATE_MINIMUM_NOT_MET", "Order does not meet the affiliate minimum")
        if await self.session.scalar(select(AffiliateConversion.id).where(AffiliateConversion.order_id == order.id)):
            raise ConflictError("AFFILIATE_ORDER_ALREADY_ATTRIBUTED", "Order already has affiliate attribution")
        amount = (
            affiliate.fixed_commission_paise if affiliate.commission_type == AffiliateCommissionType.FIXED_PAISE
            else order.total_paise * affiliate.percentage_bps // 10_000
        )
        if affiliate.max_commission_paise is not None:
            amount = min(amount, affiliate.max_commission_paise)
        if amount <= 0:
            raise ConflictError("AFFILIATE_COMMISSION_ROUNDS_TO_ZERO", "Commission would be zero paise")
        conversion = AffiliateConversion(
            id=uuid4(), affiliate_id=affiliate.id, order_id=order.id,
            buyer_user_id=order.buyer_user_id, affiliate_code_snapshot=affiliate.code,
            policy_snapshot=self.policy_snapshot(affiliate), evidence_reference=evidence_reference,
            status=AffiliateConversionStatus.PENDING_REVIEW,
            created_from_settlement_id=order.settlement_reference_id,
            attributed_by_user_id=actor_user_id,
        )
        commission = AffiliateCommission(
            id=uuid4(), conversion_id=conversion.id, affiliate_id=affiliate.id,
            beneficiary_user_id=affiliate.owner_user_id, amount_paise=amount,
            currency="INR", status=AffiliateCommissionStatus.PENDING_REVIEW,
        )
        self.session.add_all((conversion, commission))
        record = self.idempotency.record(
            actor_scope=scope, key=idempotency_key, request_fingerprint=digest,
            resource_type="affiliate_conversion", resource_id=conversion.id, status_code=201,
        )
        self.audit.record(
            actor_user_id=actor_user_id, entity_type="affiliate_conversion", entity_id=conversion.id,
            action="AFFILIATE_CONVERSION_PENDING_REVIEW", before_state=None,
            after_state=self.conversion_snapshot(conversion),
        )
        self.audit.record(
            actor_user_id=actor_user_id, entity_type="affiliate_commission", entity_id=commission.id,
            action="AFFILIATE_COMMISSION_PENDING_REVIEW", before_state=None,
            after_state=self.commission_snapshot(commission),
        )
        await self.session.flush()
        payload = self.conversion_snapshot(conversion)
        payload["commission"] = self.commission_snapshot(commission)
        record.response_payload = payload
        if commit:
            await self.session.commit()
        return conversion, commission, payload, False

    async def list_affiliates(self, *, limit: int) -> list[Affiliate]:
        return list(await self.session.scalars(
            select(Affiliate).order_by(Affiliate.created_at.desc(), Affiliate.id).limit(self._limit(limit))
        ))

    async def suspend(
        self, *, affiliate_id: UUID, actor_user_id: UUID, reason: str,
        idempotency_key: str, commit: bool,
    ) -> dict[str, object]:
        await self._require_admin(actor_user_id)
        reason = self._text(reason, 500)
        scope = f"admin:{actor_user_id}:affiliate.suspend"
        digest = fingerprint({"affiliate_id": affiliate_id, "reason": reason})
        replay = await self.idempotency.get_replay(actor_scope=scope, key=idempotency_key, request_fingerprint=digest)
        if replay is not None:
            if not isinstance(replay.response_payload, dict):
                raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior affiliate review cannot be recovered")
            return dict(replay.response_payload)
        affiliate = await self.session.scalar(select(Affiliate).where(Affiliate.id == affiliate_id).with_for_update())
        if affiliate is None:
            raise ValidationError("UNKNOWN_AFFILIATE", "Affiliate does not exist")
        if affiliate.status == AffiliateStatus.SUSPENDED:
            raise ConflictError("AFFILIATE_SUSPENDED", "Affiliate is already suspended")
        before = self.snapshot(affiliate)
        affiliate.status = AffiliateStatus.SUSPENDED
        record = self.idempotency.record(
            actor_scope=scope, key=idempotency_key, request_fingerprint=digest,
            resource_type="affiliate", resource_id=affiliate.id,
        )
        self.audit.record(
            actor_user_id=actor_user_id, entity_type="affiliate", entity_id=affiliate.id,
            action="AFFILIATE_SUSPENDED", before_state=before,
            after_state=self.snapshot(affiliate), reason=reason,
        )
        await self.session.flush()
        payload = self.snapshot(affiliate)
        record.response_payload = payload
        if commit:
            await self.session.commit()
        return payload

    async def void_conversion(
        self, *, conversion_id: UUID, actor_user_id: UUID, reason: str,
        idempotency_key: str, commit: bool,
    ) -> dict[str, object]:
        await self._require_admin(actor_user_id)
        reason = self._text(reason, 500)
        scope = f"admin:{actor_user_id}:affiliate-conversion.void"
        digest = fingerprint({"conversion_id": conversion_id, "reason": reason})
        replay = await self.idempotency.get_replay(actor_scope=scope, key=idempotency_key, request_fingerprint=digest)
        if replay is not None:
            if not isinstance(replay.response_payload, dict):
                raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior conversion review cannot be recovered")
            return dict(replay.response_payload)
        conversion = await self.session.scalar(
            select(AffiliateConversion).where(AffiliateConversion.id == conversion_id).with_for_update()
        )
        if conversion is None:
            raise ValidationError("UNKNOWN_AFFILIATE_CONVERSION", "Conversion does not exist")
        commission = await self.session.scalar(
            select(AffiliateCommission)
            .where(AffiliateCommission.conversion_id == conversion.id).with_for_update()
        )
        if commission is None:
            raise ConflictError("AFFILIATE_COMMISSION_MISSING", "Conversion is missing its commission")
        if conversion.status != AffiliateConversionStatus.PENDING_REVIEW or commission.status != AffiliateCommissionStatus.PENDING_REVIEW:
            raise ConflictError("AFFILIATE_CONVERSION_NOT_PENDING", "Conversion is not pending review")
        before_conversion = self.conversion_snapshot(conversion)
        before_commission = self.commission_snapshot(commission)
        now = datetime.now(timezone.utc)
        conversion.status = AffiliateConversionStatus.VOIDED
        conversion.voided_by_user_id = actor_user_id
        conversion.voided_at = now
        conversion.void_reason = reason
        commission.status = AffiliateCommissionStatus.VOIDED
        commission.voided_by_user_id = actor_user_id
        commission.voided_at = now
        commission.void_reason = reason
        record = self.idempotency.record(
            actor_scope=scope, key=idempotency_key, request_fingerprint=digest,
            resource_type="affiliate_conversion", resource_id=conversion.id,
        )
        self.audit.record(
            actor_user_id=actor_user_id, entity_type="affiliate_conversion", entity_id=conversion.id,
            action="AFFILIATE_CONVERSION_VOIDED", before_state=before_conversion,
            after_state=self.conversion_snapshot(conversion), reason=reason,
        )
        self.audit.record(
            actor_user_id=actor_user_id, entity_type="affiliate_commission", entity_id=commission.id,
            action="AFFILIATE_COMMISSION_VOIDED", before_state=before_commission,
            after_state=self.commission_snapshot(commission), reason=reason,
        )
        await self.session.flush()
        payload = self.conversion_snapshot(conversion)
        payload["commission"] = self.commission_snapshot(commission)
        record.response_payload = payload
        if commit:
            await self.session.commit()
        return payload

    async def list_conversions(self, *, limit: int) -> list[AffiliateConversion]:
        return list(await self.session.scalars(
            select(AffiliateConversion).order_by(AffiliateConversion.created_at.desc(), AffiliateConversion.id).limit(self._limit(limit))
        ))

    async def list_commissions(self, *, limit: int) -> list[AffiliateCommission]:
        return list(await self.session.scalars(
            select(AffiliateCommission).order_by(AffiliateCommission.created_at.desc(), AffiliateCommission.id).limit(self._limit(limit))
        ))

    async def _require_admin(self, actor_user_id: UUID) -> None:
        user = await self.session.scalar(
            select(User).options(selectinload(User.roles)).where(User.id == actor_user_id)
        )
        if user is None or not user.is_active or RoleName.ADMIN.value not in {role.name for role in user.roles}:
            raise AuthorizationError("Administrator role is required")

    @staticmethod
    def _code(value: object) -> str:
        if not isinstance(value, str):
            raise ValidationError("INVALID_AFFILIATE_CODE", "Affiliate code must be text")
        code = value.strip().upper()
        if (
            not 3 <= len(code) <= 64 or not code[0].isascii() or not code[0].isalnum()
            or not all((c.isascii() and c.isalnum()) or c in "_-" for c in code)
        ):
            raise ValidationError("INVALID_AFFILIATE_CODE", "Affiliate code is invalid")
        return code

    @staticmethod
    def _text(value: object, maximum: int) -> str:
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum:
            raise ValidationError("INVALID_AFFILIATE_TEXT", "Affiliate text is invalid")
        return value.strip()

    @staticmethod
    def _limit(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1_000:
            raise ValidationError("INVALID_LIMIT", "limit must be 1 through 1000")
        return value

    @classmethod
    def policy_snapshot(cls, affiliate: Affiliate) -> dict[str, object]:
        return canonical_payload({
            "code": affiliate.code, "commission_type": affiliate.commission_type,
            "fixed_commission_paise": affiliate.fixed_commission_paise,
            "percentage_bps": affiliate.percentage_bps,
            "max_commission_paise": affiliate.max_commission_paise,
            "minimum_order_paise": affiliate.minimum_order_paise,
        })

    @classmethod
    def snapshot(cls, affiliate: Affiliate) -> dict[str, object]:
        return canonical_payload({
            "id": affiliate.id, "code": affiliate.code, "display_name": affiliate.display_name,
            "owner_user_id": affiliate.owner_user_id, "commission_type": affiliate.commission_type,
            "fixed_commission_paise": affiliate.fixed_commission_paise,
            "percentage_bps": affiliate.percentage_bps,
            "max_commission_paise": affiliate.max_commission_paise,
            "minimum_order_paise": affiliate.minimum_order_paise, "status": affiliate.status,
            "created_by_user_id": affiliate.created_by_user_id,
            "activated_by_user_id": affiliate.activated_by_user_id,
            "activated_at": affiliate.activated_at,
        })

    @classmethod
    def conversion_snapshot(cls, conversion: AffiliateConversion) -> dict[str, object]:
        return canonical_payload({
            "id": conversion.id, "affiliate_id": conversion.affiliate_id,
            "order_id": conversion.order_id, "buyer_user_id": conversion.buyer_user_id,
            "affiliate_code_snapshot": conversion.affiliate_code_snapshot,
            "policy_snapshot": conversion.policy_snapshot,
            "evidence_reference": conversion.evidence_reference,
            "status": conversion.status,
            "created_from_settlement_id": conversion.created_from_settlement_id,
            "attributed_by_user_id": conversion.attributed_by_user_id,
            "voided_at": conversion.voided_at, "void_reason": conversion.void_reason,
        })

    @classmethod
    def commission_snapshot(cls, commission: AffiliateCommission) -> dict[str, object]:
        return canonical_payload({
            "id": commission.id, "conversion_id": commission.conversion_id,
            "affiliate_id": commission.affiliate_id,
            "beneficiary_user_id": commission.beneficiary_user_id,
            "amount_paise": commission.amount_paise, "currency": commission.currency,
            "status": commission.status,
            "voided_at": commission.voided_at, "void_reason": commission.void_reason,
        })
