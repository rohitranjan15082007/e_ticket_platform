"""Admin-only creation of P2P refund evidence cases.

This endpoint records a pending case only; it cannot initiate or confirm an
external payout and it never releases the receiver's already-settled hold.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, status

from app.core.permissions import RoleName
from app.dependencies import SessionDependency, require_roles
from app.models.user import User
from app.schemas.payment import RefundCaseCreateRequest, RefundCaseResponse
from app.services.refund_service import RefundService


router = APIRouter(prefix="/admin/refunds", tags=["admin p2p refunds"])
AdminUser = Annotated[User, Depends(require_roles(RoleName.ADMIN))]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=255)]


@router.post("", response_model=RefundCaseResponse, status_code=status.HTTP_201_CREATED)
async def create_refund_case(
    payload: RefundCaseCreateRequest,
    session: SessionDependency,
    current_user: AdminUser,
    idempotency_key: IdempotencyKey,
) -> RefundCaseResponse:
    result = await RefundService(session).create_pending_case(
        match_id=payload.match_id,
        actor_user_id=current_user.id,
        amount_paise=payload.amount_paise,
        currency=payload.currency,
        reason=payload.reason,
        liable_party=payload.liable_party,
        funding_source_reference=payload.funding_source_reference,
        destination_validation_reference=payload.destination_validation_reference,
        executor_reference=payload.executor_reference,
        idempotency_key=idempotency_key,
        commit=True,
    )
    return RefundCaseResponse.model_validate(result.response_payload)
