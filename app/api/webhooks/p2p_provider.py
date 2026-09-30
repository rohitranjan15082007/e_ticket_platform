"""Signed provider callback endpoint for P2P payment evidence."""

from typing import Annotated

from fastapi import APIRouter, Header, Request, status
from pydantic import ValidationError as PydanticValidationError

from app.dependencies import SessionDependency
from app.exceptions import ValidationError
from app.schemas.provider_webhook import P2PProviderWebhookRequest, P2PProviderWebhookResponse
from app.services.provider_event_service import ProviderEventService


router = APIRouter(tags=["p2p provider webhooks"])
ProviderSignature = Annotated[str | None, Header(alias="X-P2P-Signature")]


@router.post(
    "/payments/provider-webhook",
    response_model=P2PProviderWebhookResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def ingest_p2p_provider_webhook(
    request: Request,
    session: SessionDependency,
    signature: ProviderSignature = None,
) -> P2PProviderWebhookResponse:
    """Verify raw-body HMAC before retaining and processing a provider event."""

    raw_body = await request.body()
    try:
        payload = P2PProviderWebhookRequest.model_validate_json(raw_body)
    except PydanticValidationError as error:
        raise ValidationError("INVALID_PROVIDER_EVENT", "Provider webhook payload is invalid") from error
    result = await ProviderEventService(session).ingest_signed_event(
        payload=payload,
        raw_body=raw_body,
        signature=signature,
        commit=True,
    )
    return P2PProviderWebhookResponse.model_validate(
        {**result.response_payload, "replayed": result.replayed}
    )
