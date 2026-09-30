"""Evidence-required administrative P2P dispute resolution facade."""

from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditService
from app.core.idempotency import IdempotencyService, fingerprint
from app.exceptions import ConflictError, ValidationError
from app.models.p2p_match import (
    AdminResolutionDecision,
    P2PAdminResolution,
    P2PDisputeStatus,
    P2PMatchStatus,
)
from app.repositories.payment_repository import get_dispute
from app.services.p2p_service import P2PMatchMutationResult, P2PService


class DisputeService:
    """Routes all resolution outcomes through the same safe P2P transitions."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.audit = AuditService(session)
        self.idempotency = IdempotencyService(session)
        self.p2p = P2PService(session)

    async def resolve(
        self,
        *,
        dispute_id: UUID,
        actor_user_id: UUID,
        decision: AdminResolutionDecision,
        payment_submission_id: UUID | None,
        reason: str,
        evidence_reference: str,
        verification_source: str,
        release_hold: bool,
        idempotency_key: str,
        commit: bool,
    ) -> P2PMatchMutationResult:
        await self.p2p._require_admin(actor_user_id)
        # Read the match link first, then acquire the P2P context locks before
        # locking the dispute row.  Direct P2P resolution paths use the same
        # order (order -> withdrawal -> wallet -> match -> dispute), avoiding
        # a PostgreSQL lock cycle between the admin endpoints.
        observed_dispute = await get_dispute(self.session, dispute_id)
        if observed_dispute is None:
            raise ValidationError("UNKNOWN_DISPUTE", "P2P dispute does not exist")
        if decision == AdminResolutionDecision.SETTLE:
            if payment_submission_id is None:
                raise ValidationError("PAYMENT_PROOF_REQUIRED", "Settlement override requires the original payment claim")
            return await self.p2p.admin_settle(
                match_id=observed_dispute.match_id,
                actor_user_id=actor_user_id,
                payment_submission_id=payment_submission_id,
                reason=reason,
                evidence_reference=evidence_reference,
                verification_source=verification_source,
                dispute_id=observed_dispute.id,
                idempotency_key=idempotency_key,
                commit=commit,
            )
        if decision == AdminResolutionDecision.CLOSE_UNPAID:
            return await self.p2p.close_unpaid(
                match_id=observed_dispute.match_id,
                actor_user_id=actor_user_id,
                reason=reason,
                evidence_reference=evidence_reference,
                release_hold=release_hold,
                idempotency_key=idempotency_key,
                commit=commit,
            )
        if decision != AdminResolutionDecision.KEEP_IN_REVIEW:
            raise ValidationError("INVALID_RESOLUTION", "Unsupported dispute resolution decision")
        context = await self.p2p._locked_match_context(observed_dispute.match_id)
        dispute = await get_dispute(self.session, dispute_id, for_update=True)
        if dispute is None or dispute.match_id != context.match.id:
            raise ValidationError("UNKNOWN_DISPUTE", "P2P dispute does not belong to this match")
        if dispute.status == P2PDisputeStatus.RESOLVED:
            raise ConflictError("INVALID_TRANSITION", "P2P dispute is already resolved")
        scope = f"admin:{actor_user_id}:p2p.dispute-review"
        request_fingerprint = fingerprint(
            {"dispute_id": dispute_id, "reason": reason, "evidence_reference": evidence_reference}
        )
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self.p2p._replay_match(replay)
        before = self.p2p.match_state(context.match)
        before_dispute = self.p2p.dispute_state(dispute)
        self.p2p._move_match_to_admin_review(context.match)
        self.p2p._transition_dispute(dispute, P2PDisputeStatus.UNDER_REVIEW)
        resolution = P2PAdminResolution(
            id=uuid4(),
            match_id=context.match.id,
            dispute_id=dispute.id,
            actor_user_id=actor_user_id,
            decision=AdminResolutionDecision.KEEP_IN_REVIEW,
            reason=reason,
            evidence_reference=evidence_reference,
            verification_source=verification_source,
        )
        self.session.add(resolution)
        record = self.idempotency.record(
            actor_scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint,
            resource_type="p2p_dispute",
            resource_id=dispute.id,
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="p2p_match",
            entity_id=context.match.id,
            action="P2P_DISPUTE_ESCALATED_TO_ADMIN_REVIEW",
            before_state=before,
            after_state=self.p2p.match_state(context.match),
            reason=reason,
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="p2p_dispute",
            entity_id=dispute.id,
            action="P2P_DISPUTE_UNDER_ADMIN_REVIEW",
            before_state=before_dispute,
            after_state=self.p2p.dispute_state(dispute),
            reason=reason,
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="p2p_admin_resolution",
            entity_id=resolution.id,
            action="P2P_ADMIN_REVIEW_RECORDED",
            before_state=None,
            after_state=self.p2p.admin_resolution_state(resolution),
            reason=reason,
        )
        response = self.p2p.match_snapshot(context.match, include_destination=False)
        record.response_payload = response
        await self.p2p._finish(commit=commit)
        return P2PMatchMutationResult(context.match, response, False)
