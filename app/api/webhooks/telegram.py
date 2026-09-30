"""Telegram Stars webhook trust boundary and pre-checkout acknowledgement."""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import ValidationError as PydanticValidationError

from app.dependencies import SessionDependency
from app.exceptions import ValidationError
from app.models.payment import PaymentProviderEventStatus
from app.payment_adapters.base import PaymentAdapterError
from app.schemas.payment_methods import TelegramUpdate, TelegramWebhookResponse
from app.services.payment_orchestrator import PaymentOrchestrator


router = APIRouter(prefix="/webhooks/telegram", tags=["Telegram Stars webhooks"])
TelegramWebhookSecret = Annotated[
    str | None,
    Header(alias="X-Telegram-Bot-Api-Secret-Token"),
]
PRE_CHECKOUT_ACK_BUDGET_SECONDS = 9.0


@router.post("", response_model=TelegramWebhookResponse, status_code=status.HTTP_202_ACCEPTED)
async def ingest_telegram_webhook(
    request: Request,
    session: SessionDependency,
    webhook_secret: TelegramWebhookSecret = None,
) -> TelegramWebhookResponse:
    """Retain an authenticated update and settle only successful payments.

    A pre-checkout query is acknowledged only after immutable payment facts are
    persisted and validated. If outbound acknowledgement fails, returning 503
    asks Telegram to retry the same durable update.
    """

    raw_body = await request.body()
    try:
        payload = TelegramUpdate.model_validate_json(raw_body)
    except PydanticValidationError as error:
        raise ValidationError("INVALID_TELEGRAM_UPDATE", "Telegram webhook payload is invalid") from error

    query = payload.pre_checkout_query

    async def ingest_and_respond(*, deadline: float | None = None) -> TelegramWebhookResponse:
        service = PaymentOrchestrator(session)
        result = await service.ingest_telegram_update(
            payload=payload,
            raw_body=raw_body,
            webhook_secret=webhook_secret,
            commit=True,
        )
        response_payload: dict[str, object] = {
            **result.response_payload,
            "replayed": result.replayed,
            "update_id": payload.update_id,
            "pre_checkout_query_id": result.pre_checkout_query_id,
            "pre_checkout_approved": result.pre_checkout_approved,
            "pre_checkout_error": result.pre_checkout_error,
        }

        if query is not None:
            # A retry has already persisted the same update, so reconstruct
            # the acknowledgement decision from durable status and answer it
            # again.  Bound Bot API time by the remaining end-to-end budget:
            # Telegram requires this answer promptly, while the event itself
            # remains safely replayable if the deadline is missed.
            approved = result.pre_checkout_approved
            if approved is None:
                approved = result.event.status == PaymentProviderEventStatus.PROCESSED
            error_message = result.pre_checkout_error
            if not approved and error_message is None:
                error_message = "Payment details do not match this order."
            timeout_seconds = 8.0
            if deadline is not None:
                timeout_seconds = min(8.0, max(0.1, deadline - asyncio.get_running_loop().time()))
            try:
                await service.telegram_adapter().answer_pre_checkout_query(
                    pre_checkout_query_id=query.id,
                    ok=approved,
                    error_message=error_message if not approved else None,
                    timeout_seconds=timeout_seconds,
                )
            except PaymentAdapterError as error:
                # The event is committed before this point. A retry is safer
                # than silently losing Telegram's time-bounded acknowledgement.
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Telegram pre-checkout acknowledgement could not be delivered",
                ) from error
            response_payload.update(
                {
                    "pre_checkout_query_id": query.id,
                    "pre_checkout_approved": approved,
                    "pre_checkout_error": error_message,
                }
            )
        return TelegramWebhookResponse.model_validate(response_payload)

    if query is None:
        return await ingest_and_respond()

    deadline = asyncio.get_running_loop().time() + PRE_CHECKOUT_ACK_BUDGET_SECONDS
    try:
        async with asyncio.timeout(PRE_CHECKOUT_ACK_BUDGET_SECONDS):
            return await ingest_and_respond(deadline=deadline)
    except TimeoutError as error:
        # A cancellation during database work must not leave a transaction
        # open.  A prior successful commit is unaffected; Telegram can retry
        # the same update and use its durable replay decision.
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram pre-checkout acknowledgement exceeded its deadline",
        ) from error
