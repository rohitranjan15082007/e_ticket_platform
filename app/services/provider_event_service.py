"""Signed, replay-safe provider-event ingestion for the P2P workflow."""

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.audit import AuditService
from app.core.idempotency import canonical_payload
from app.core.transaction_locks import lock_key_for_transaction
from app.exceptions import AppError, AuthorizationError, ValidationError
from app.models.p2p_match import P2PProviderEvent, P2PProviderEventStatus
from app.payment_adapters.p2p_adapter import HmacP2PWebhookAdapter
from app.repositories.payment_repository import get_match, get_submission
from app.repositories.provider_event_repository import get_provider_event
from app.schemas.provider_webhook import P2PProviderWebhookRequest
from app.services.p2p_service import P2PService


@dataclass(frozen=True, slots=True)
class ProviderEventMutationResult:
    event: P2PProviderEvent
    response_payload: dict[str, object]
    replayed: bool


class ProviderEventService:
    """Record a callback before it can influence evidence or settlement.

    This service deliberately has no generic gateway verification fallback.  A
    callback must be HMAC-authenticated and identify the exact frozen P2P
    recipient, reference, amount, and currency; anything uncertain becomes a
    durable review record instead of a payment success.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.audit = AuditService(session)
        self.p2p = P2PService(session)

    async def ingest_signed_event(
        self,
        *,
        payload: P2PProviderWebhookRequest,
        raw_body: bytes,
        signature: str | None,
        commit: bool,
    ) -> ProviderEventMutationResult:
        self._verify_signature(raw_body, signature)
        payload_digest = sha256(raw_body).hexdigest()
        provider_namespace = payload.provider_namespace
        external_event_id = payload.external_event_id
        await lock_key_for_transaction(
            self.session, "p2p-provider-event", provider_namespace, external_event_id
        )
        existing = await get_provider_event(
            self.session,
            provider_namespace=provider_namespace,
            external_event_id=external_event_id,
            for_update=True,
        )
        if existing is not None:
            if existing.payload_digest != payload_digest:
                before = self._event_state(existing)
                existing.status = P2PProviderEventStatus.REVIEW_REQUIRED
                existing.processing_error = "REPLAY_PAYLOAD_DIGEST_CONFLICT"
                existing.processed_at = datetime.now(timezone.utc)
                self.audit.record(
                    actor_user_id=None,
                    entity_type="p2p_provider_event",
                    entity_id=existing.id,
                    action="P2P_PROVIDER_EVENT_REPLAY_CONFLICT",
                    before_state=before,
                    after_state=self._event_state(existing),
                    reason="The same external event ID arrived with a different signed payload digest",
                )
                await self._finish(commit=commit)
                return ProviderEventMutationResult(existing, self._response(existing), False)
            return ProviderEventMutationResult(existing, self._response(existing), True)

        # Keep signed unknown/out-of-order callbacks durable. The optional
        # foreign-key columns record links only when their target exists; the
        # immutable raw payload keeps the reported identifiers for review.
        reported_match = await get_match(self.session, payload.match_id)
        reported_submission = await get_submission(self.session, payload.payment_submission_id)
        now = datetime.now(timezone.utc)
        event = P2PProviderEvent(
            provider_namespace=provider_namespace,
            external_event_id=external_event_id,
            event_type=payload.event_type,
            payload_digest=payload_digest,
            payload=canonical_payload(payload.model_dump(mode="json")),
            match_id=reported_match.id if reported_match is not None else None,
            payment_submission_id=reported_submission.id if reported_submission is not None else None,
            transaction_reference=payload.transaction_reference,
            verified_amount_paise=payload.verified_amount_paise,
            verified_currency=payload.verified_currency,
            recipient_fingerprint=payload.recipient_fingerprint,
            occurred_at=self._as_utc(payload.occurred_at),
            status=P2PProviderEventStatus.PROCESSING,
        )
        self.session.add(event)
        await self.session.flush()
        before = self._event_state(event)
        settlement_id = None
        try:
            if reported_match is None:
                raise ValidationError("UNKNOWN_MATCH", "Provider event names a P2P match that does not exist")
            if reported_submission is None:
                raise ValidationError(
                    "UNKNOWN_PAYMENT_SUBMISSION",
                    "Provider event names a payment submission that does not exist",
                )
            if payload.event_type == "PAYMENT_VERIFIED":
                # Verification and any settlement it authorizes must persist as
                # one unit: a domain failure inside this savepoint rolls back
                # the submission status and verified-reference writes so a
                # "verified" submission can never be committed without the
                # settlement it implied.  The durable provider event itself was
                # flushed before the savepoint and survives for review.
                async with self.session.begin_nested():
                    result = await self.p2p.apply_trusted_provider_verification(
                        match_id=payload.match_id,
                        payment_submission_id=payload.payment_submission_id,
                        provider_namespace=provider_namespace,
                        transaction_reference=payload.transaction_reference,
                        verified_amount_paise=payload.verified_amount_paise,
                        verified_currency=payload.verified_currency,
                        recipient_fingerprint=payload.recipient_fingerprint,
                        provider_event_id=external_event_id,
                        verification_source=f"PROVIDER_WEBHOOK:{provider_namespace}",
                        commit=False,
                    )
                payload_result = result.response_payload
                if payload_result.get("review_required") is True:
                    event.status = P2PProviderEventStatus.REVIEW_REQUIRED
                    event.processing_error = str(payload_result.get("code", "REVIEW_REQUIRED"))
                else:
                    event.status = P2PProviderEventStatus.PROCESSED
                    settlement_value = payload_result.get("settlement")
                    if isinstance(settlement_value, dict):
                        settlement_id = settlement_value.get("id")
            elif payload.event_type == "PAYMENT_PENDING":
                # Retain a trusted pending event without converting it into
                # either a success or a rejection.
                event.status = P2PProviderEventStatus.PROCESSED
            else:
                # A provider rejection/failure is evidence, not permission to
                # release exposed funds automatically.  An operator reviews it.
                event.status = P2PProviderEventStatus.REVIEW_REQUIRED
                event.processing_error = "PROVIDER_REJECTED_REQUIRES_RECONCILIATION"
        except AppError as error:
            # Unknown/late/out-of-order events must stay durable and visible to
            # reconciliation staff.  Do not lose the signed evidence by
            # re-raising a domain response and rolling this transaction back.
            event.status = P2PProviderEventStatus.REVIEW_REQUIRED
            event.processing_error = self._error_text(error.code, error.message)
        event.processed_at = now
        self.audit.record(
            actor_user_id=None,
            entity_type="p2p_provider_event",
            entity_id=event.id,
            action="P2P_PROVIDER_EVENT_PROCESSED",
            before_state=before,
            after_state=self._event_state(event),
            reason=event.processing_error,
        )
        await self._finish(commit=commit)
        response = self._response(event, settlement_id=settlement_id)
        return ProviderEventMutationResult(event, response, False)

    @staticmethod
    def _verify_signature(raw_body: bytes, signature: str | None) -> None:
        secret = get_settings().p2p_provider_webhook_secret
        if not secret:
            raise AuthorizationError("P2P provider webhook is disabled until its signing secret is configured")
        HmacP2PWebhookAdapter(signing_secret=secret).verify_webhook_signature(
            raw_body=raw_body, signature=signature
        )

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    @staticmethod
    def _error_text(code: str, message: str) -> str:
        return f"{code}: {message}"[:500]

    @classmethod
    def _event_state(cls, event: P2PProviderEvent) -> dict[str, object]:
        return {
            "id": event.id,
            "provider_namespace": event.provider_namespace,
            "external_event_id": event.external_event_id,
            "event_type": event.event_type,
            "payload_digest": event.payload_digest,
            "match_id": event.match_id,
            "payment_submission_id": event.payment_submission_id,
            "transaction_reference": event.transaction_reference,
            "status": event.status,
            "processing_error": event.processing_error,
        }

    @classmethod
    def _response(
        cls, event: P2PProviderEvent, *, settlement_id: object | None = None
    ) -> dict[str, object]:
        response: dict[str, object] = {
            "id": event.id,
            "status": event.status,
            "review_required": event.status == P2PProviderEventStatus.REVIEW_REQUIRED,
            "settlement_id": settlement_id,
        }
        return canonical_payload(response)

    async def _finish(self, *, commit: bool) -> None:
        await self.session.flush()
        if commit:
            await self.session.commit()
