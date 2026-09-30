"""Signed callbacks for a configured white-label payment provider."""

from typing import Annotated

from fastapi import APIRouter, Header, Request, status
from pydantic import ValidationError as PydanticValidationError

from app.dependencies import SessionDependency
from app.exceptions import ValidationError
from app.schemas.payment_methods import PaymentWebhookResponse, WhiteLabelWebhookRequest
from app.services.payment_orchestrator import PaymentOrchestrator


router = APIRouter(prefix="/webhooks/white-label", tags=["white-label payment webhooks"])
WhiteLabelSignature = Annotated[str | None, Header(alias="X-White-Label-Signature")]


@router.post("/{provider_namespace}", response_model=PaymentWebhookResponse, status_code=status.HTTP_202_ACCEPTED)
async def ingest_white_label_webhook(
    provider_namespace: str,
    request: Request,
    session: SessionDependency,
    signature: WhiteLabelSignature = None,
) -> PaymentWebhookResponse:
    """Verify the exact raw payload before retaining a provider event once."""

    raw_body = await request.body()
    try:
        payload = WhiteLabelWebhookRequest.model_validate_json(raw_body)
    except PydanticValidationError as error:
        raise ValidationError("INVALID_WHITE_LABEL_EVENT", "White-label webhook payload is invalid") from error
    if payload.provider_namespace != provider_namespace:
        raise ValidationError(
            "PROVIDER_NAMESPACE_PATH_MISMATCH",
            "Webhook path provider namespace does not match its signed payload",
        )
    result = await PaymentOrchestrator(session).ingest_white_label_event(
        payload=payload,
        raw_body=raw_body,
        signature=signature,
        request_headers=dict(request.headers),
        commit=True,
    )
    return PaymentWebhookResponse.model_validate(
        {**result.response_payload, "replayed": result.replayed}
    )
