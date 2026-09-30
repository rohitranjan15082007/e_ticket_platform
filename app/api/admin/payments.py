"""Administrator P2P and manual-payment review endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query

from app.core.permissions import RoleName
from app.dependencies import SessionDependency, require_roles
from app.models.user import User
from app.schemas.payment import CloseUnpaidRequest, DeliveryRetryResponse, P2PQueueResponse
from app.schemas.payment_methods import (
    ManualPaymentDestinationCreateRequest,
    ManualPaymentDestinationResponse,
    ManualPaymentProofResponse,
    ManualPaymentReviewRequest,
    ManualPaymentReviewResponse,
)
from app.services.p2p_service import P2PService
from app.services.payment_orchestrator import PaymentOrchestrator


router = APIRouter(prefix="/admin", tags=["admin p2p payments"])
AdminUser = Annotated[User, Depends(require_roles(RoleName.ADMIN))]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=255)]


@router.post("/matches/{match_id}/close-unpaid", response_model=P2PQueueResponse)
async def close_unpaid_match(
    match_id: UUID,
    payload: CloseUnpaidRequest,
    session: SessionDependency,
    current_user: AdminUser,
    idempotency_key: IdempotencyKey,
) -> P2PQueueResponse:
    result = await P2PService(session).close_unpaid(
        match_id=match_id,
        actor_user_id=current_user.id,
        reason=payload.reason,
        evidence_reference=payload.evidence_reference,
        release_hold=payload.release_hold,
        idempotency_key=idempotency_key,
        commit=True,
    )
    return P2PQueueResponse.model_validate(result.response_payload)


@router.post("/orders/{order_id}/retry-delivery", response_model=DeliveryRetryResponse)
async def retry_order_delivery(
    order_id: UUID,
    session: SessionDependency,
    current_user: AdminUser,
    idempotency_key: IdempotencyKey,
) -> DeliveryRetryResponse:
    response = await P2PService(session).retry_delivery(
        order_id=order_id,
        actor_user_id=current_user.id,
        idempotency_key=idempotency_key,
        commit=True,
    )
    return DeliveryRetryResponse.model_validate(response)


@router.post(
    "/manual-upi/destinations",
    response_model=ManualPaymentDestinationResponse,
    status_code=201,
)
async def create_manual_upi_destination(
    payload: ManualPaymentDestinationCreateRequest,
    session: SessionDependency,
    current_user: AdminUser,
    idempotency_key: IdempotencyKey,
) -> ManualPaymentDestinationResponse:
    """Create the one active, audited manual UPI/QR destination."""

    response = await PaymentOrchestrator(session).create_manual_destination(
        actor_user_id=current_user.id,
        display_label=payload.display_label,
        upi_id=payload.upi_id,
        qr_reference=payload.qr_reference,
        instructions=payload.instructions,
        approval_evidence_reference=payload.approval_evidence_reference,
        idempotency_key=idempotency_key,
        commit=True,
    )
    return ManualPaymentDestinationResponse.model_validate(response)


@router.get("/manual-upi/review", response_model=list[ManualPaymentProofResponse])
async def list_manual_upi_proofs_for_review(
    session: SessionDependency,
    current_user: AdminUser,
    limit: int = Query(default=100, ge=1, le=1_000),
) -> list[ManualPaymentProofResponse]:
    """List payment evidence awaiting an administrator decision."""

    proofs = await PaymentOrchestrator(session).list_manual_proofs_for_review(
        actor_user_id=current_user.id,
        limit=limit,
    )
    return [
        ManualPaymentProofResponse.model_validate(PaymentOrchestrator.manual_proof_snapshot(proof))
        for proof in proofs
    ]


@router.post("/manual-upi/{attempt_id}/review", response_model=ManualPaymentReviewResponse)
async def review_manual_upi_proof(
    attempt_id: UUID,
    payload: ManualPaymentReviewRequest,
    session: SessionDependency,
    current_user: AdminUser,
    idempotency_key: IdempotencyKey,
) -> ManualPaymentReviewResponse:
    """Apply an explicit admin review decision; only approval may settle payment."""

    result = await PaymentOrchestrator(session).review_manual_proof(
        attempt_id=attempt_id,
        actor_user_id=current_user.id,
        decision=payload.decision.value,
        review_note=payload.review_note,
        idempotency_key=idempotency_key,
        commit=True,
    )
    return ManualPaymentReviewResponse.model_validate(
        {**result.response_payload, "replayed": result.replayed}
    )
