"""Evidence-required administrator resolution for P2P disputes."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy import select

from app.core.permissions import RoleName
from app.dependencies import SessionDependency, require_roles
from app.models.user import User
from app.models.p2p_match import P2PDispute, P2PDisputeStatus
from app.schemas.admin import AdminDisputeResponse
from app.schemas.payment import AdminDisputeResolveRequest, P2PQueueResponse, P2PSettlementResponse
from app.services.dispute_service import DisputeService


router = APIRouter(prefix="/admin/disputes", tags=["admin p2p disputes"])
AdminUser = Annotated[User, Depends(require_roles(RoleName.ADMIN))]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=255)]


@router.get("", response_model=list[AdminDisputeResponse])
async def list_disputes_for_review(
    session: SessionDependency, current_user: AdminUser,
    limit: int = Query(default=100, ge=1, le=200),
    status: P2PDisputeStatus | None = None,
) -> list[AdminDisputeResponse]:
    statement = select(P2PDispute)
    if status is not None:
        statement = statement.where(P2PDispute.status == status)
    rows = await session.scalars(
        statement.order_by(P2PDispute.created_at.desc(), P2PDispute.id.desc()).limit(limit)
    )
    return [AdminDisputeResponse.model_validate(row, from_attributes=True) for row in rows]


@router.post("/{dispute_id}/resolve")
async def resolve_dispute(
    dispute_id: UUID,
    payload: AdminDisputeResolveRequest,
    session: SessionDependency,
    current_user: AdminUser,
    idempotency_key: IdempotencyKey,
) -> P2PSettlementResponse | P2PQueueResponse | dict[str, object]:
    """Select a typed response after the service enforces the requested transition."""

    result = await DisputeService(session).resolve(
        dispute_id=dispute_id,
        actor_user_id=current_user.id,
        decision=payload.decision,
        payment_submission_id=payload.payment_submission_id,
        reason=payload.reason,
        evidence_reference=payload.evidence_reference,
        verification_source=payload.verification_source,
        release_hold=payload.release_hold,
        idempotency_key=idempotency_key,
        commit=True,
    )
    payload_result = result.response_payload
    if "settlement" in payload_result:
        return P2PSettlementResponse.model_validate(payload_result)
    if "order" in payload_result:
        return P2PQueueResponse.model_validate(payload_result)
    return payload_result
