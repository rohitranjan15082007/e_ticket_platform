"""Authenticated P2P and order-linked payment endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header

from app.dependencies import SessionDependency, get_current_user
from app.models.user import User
from app.schemas.payment import (
    P2PMatchResponse,
    P2PQueueResponse,
    PaymentSubmissionCreateRequest,
    PaymentSubmissionMutationResponse,
    ReceiverConfirmationMutationResponse,
    ReceiverConfirmationRequest,
)
from app.schemas.payment_methods import (
    ManualPaymentProofCreateRequest,
    ManualProofMutationResponse,
    PaymentMethodAvailabilityResponse,
    PaymentAttemptResponse,
    PaymentStartRequest,
    PaymentStartResponse,
)
from app.services.p2p_service import P2PService
from app.services.payment_orchestrator import PaymentOrchestrator


router = APIRouter(tags=["p2p payments"])
CurrentUser = Annotated[User, Depends(get_current_user)]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=255)]


@router.get("/payment-methods", response_model=PaymentMethodAvailabilityResponse)
async def available_payment_methods(
    session: SessionDependency, current_user: CurrentUser,
) -> PaymentMethodAvailabilityResponse:
    """Show configured checkout paths; starting one still revalidates the order."""
    methods = await PaymentOrchestrator(session).available_methods_for_buyer()
    return PaymentMethodAvailabilityResponse.model_validate(methods)


@router.post("/orders/{order_id}/p2p/match", response_model=P2PQueueResponse)
async def queue_or_match_order(
    order_id: UUID,
    session: SessionDependency,
    current_user: CurrentUser,
    idempotency_key: IdempotencyKey,
) -> P2PQueueResponse:
    result = await P2PService(session).queue_or_match_order(
        order_id=order_id,
        buyer_user_id=current_user.id,
        idempotency_key=idempotency_key,
        commit=True,
    )
    return P2PQueueResponse.model_validate(result.response_payload)


@router.get("/matches/{match_id}", response_model=P2PMatchResponse)
async def get_match(match_id: UUID, session: SessionDependency, current_user: CurrentUser) -> P2PMatchResponse:
    response = await P2PService(session).get_match_snapshot_for_actor(
        match_id=match_id, actor_user_id=current_user.id
    )
    return P2PMatchResponse.model_validate(response)


@router.post("/matches/{match_id}/payment-submissions", response_model=PaymentSubmissionMutationResponse)
async def submit_payment_claim(
    match_id: UUID,
    payload: PaymentSubmissionCreateRequest,
    session: SessionDependency,
    current_user: CurrentUser,
    idempotency_key: IdempotencyKey,
) -> PaymentSubmissionMutationResponse:
    result = await P2PService(session).submit_payment(
        match_id=match_id,
        buyer_user_id=current_user.id,
        provider_namespace=payload.provider_namespace,
        claimed_reference=payload.claimed_reference,
        observed_amount_paise=payload.observed_amount_paise,
        observed_currency=payload.observed_currency,
        declared_paid_at=payload.declared_paid_at,
        evidence_upload_id=payload.evidence_upload_id,
        provider_evidence_reference=payload.provider_evidence_reference,
        evidence_metadata=payload.evidence_metadata,
        idempotency_key=idempotency_key,
        commit=True,
    )
    return PaymentSubmissionMutationResponse.model_validate(result.response_payload)


@router.post(
    "/matches/{match_id}/receiver-confirmation", response_model=ReceiverConfirmationMutationResponse
)
async def confirm_receiver_receipt(
    match_id: UUID,
    payload: ReceiverConfirmationRequest,
    session: SessionDependency,
    current_user: CurrentUser,
    idempotency_key: IdempotencyKey,
) -> ReceiverConfirmationMutationResponse:
    result = await P2PService(session).confirm_receipt(
        match_id=match_id,
        receiver_user_id=current_user.id,
        decision=payload.decision,
        reason=payload.reason,
        idempotency_key=idempotency_key,
        commit=True,
    )
    return ReceiverConfirmationMutationResponse.model_validate(result.response_payload)


@router.post("/orders/{order_id}/payments", response_model=PaymentStartResponse)
async def start_order_payment(
    order_id: UUID,
    payload: PaymentStartRequest,
    session: SessionDependency,
    current_user: CurrentUser,
    idempotency_key: IdempotencyKey,
) -> PaymentStartResponse:
    """Expose one configured payment method for an unexposed buyer order.

    The orchestration service owns method availability, payment instructions,
    idempotency, and the order state transition. This endpoint does not accept
    client-side payment-success assertions.
    """

    result = await PaymentOrchestrator(session).create_payment_attempt(
        order_id=order_id,
        buyer_user_id=current_user.id,
        method=payload.method,
        telegram_user_id=payload.telegram_user_id,
        idempotency_key=idempotency_key,
        commit=True,
    )
    return PaymentStartResponse.model_validate(
        {**result.response_payload, "replayed": result.replayed}
    )


@router.get("/payments/{attempt_id}", response_model=PaymentAttemptResponse)
async def get_payment_attempt(
    attempt_id: UUID,
    session: SessionDependency,
    current_user: CurrentUser,
) -> PaymentAttemptResponse:
    """Return a buyer's own payment attempt without exposing another buyer's data."""

    attempt = await PaymentOrchestrator(session).get_payment_attempt_for_buyer(
        attempt_id=attempt_id,
        buyer_user_id=current_user.id,
    )
    return PaymentAttemptResponse.model_validate(PaymentOrchestrator.attempt_snapshot(attempt))


@router.post("/payments/{attempt_id}/manual-proof", response_model=ManualProofMutationResponse)
async def submit_manual_payment_proof(
    attempt_id: UUID,
    payload: ManualPaymentProofCreateRequest,
    session: SessionDependency,
    current_user: CurrentUser,
    idempotency_key: IdempotencyKey,
) -> ManualProofMutationResponse:
    """Store UTR/screenshot evidence and place it in review; never settle it."""

    result = await PaymentOrchestrator(session).submit_manual_proof(
        attempt_id=attempt_id,
        buyer_user_id=current_user.id,
        utr=payload.utr,
        proof_reference=payload.proof_reference,
        proof_metadata=payload.proof_metadata,
        submitted_amount_paise=payload.submitted_amount_paise,
        submitted_currency=payload.submitted_currency,
        idempotency_key=idempotency_key,
        commit=True,
    )
    return ManualProofMutationResponse.model_validate(
        {**result.response_payload, "replayed": result.replayed}
    )
