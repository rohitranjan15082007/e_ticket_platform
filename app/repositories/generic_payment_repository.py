"""Locked persistence helpers for Phase 5 provider-agnostic payments."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.payment import (
    ACTIVE_PAYMENT_ATTEMPT_STATUSES,
    ManualPaymentDestination,
    ManualPaymentDestinationStatus,
    ManualPaymentProof,
    PaymentAttempt,
    PaymentAttemptStatus,
    PaymentMethod,
    PaymentOutboxEvent,
    PaymentOutboxEventStatus,
    PaymentProviderEvent,
    PaymentProviderEventStatus,
    PaymentReconciliationRun,
    PaymentSettlement,
)


def _locked(statement, for_update: bool):
    return statement.with_for_update() if for_update else statement


async def get_payment_attempt(
    session: AsyncSession, attempt_id: UUID, *, for_update: bool = False
) -> PaymentAttempt | None:
    return await session.scalar(
        _locked(select(PaymentAttempt).where(PaymentAttempt.id == attempt_id), for_update)
    )


async def get_payment_attempt_for_buyer(
    session: AsyncSession, *, attempt_id: UUID, buyer_user_id: UUID, for_update: bool = False
) -> PaymentAttempt | None:
    return await session.scalar(
        _locked(
            select(PaymentAttempt).where(
                PaymentAttempt.id == attempt_id,
                PaymentAttempt.buyer_user_id == buyer_user_id,
            ),
            for_update,
        )
    )


async def get_active_payment_attempt_for_order(
    session: AsyncSession, *, order_id: UUID, for_update: bool = False
) -> PaymentAttempt | None:
    return await session.scalar(
        _locked(
            select(PaymentAttempt)
            .where(PaymentAttempt.order_id == order_id, PaymentAttempt.status.in_(ACTIVE_PAYMENT_ATTEMPT_STATUSES))
            .order_by(PaymentAttempt.created_at, PaymentAttempt.id),
            for_update,
        )
    )


async def get_payment_attempt_by_merchant_reference(
    session: AsyncSession, *, merchant_reference: str, for_update: bool = False
) -> PaymentAttempt | None:
    return await session.scalar(
        _locked(
            select(PaymentAttempt).where(PaymentAttempt.merchant_reference == merchant_reference), for_update
        )
    )


async def get_payment_attempt_by_telegram_payload(
    session: AsyncSession, *, invoice_payload: str, for_update: bool = False
) -> PaymentAttempt | None:
    return await session.scalar(
        _locked(
            select(PaymentAttempt).where(PaymentAttempt.telegram_invoice_payload == invoice_payload), for_update
        )
    )


async def get_payment_attempt_by_provider_reference(
    session: AsyncSession,
    *,
    provider_namespace: str,
    provider_payment_reference: str,
    for_update: bool = False,
) -> PaymentAttempt | None:
    return await session.scalar(
        _locked(
            select(PaymentAttempt).where(
                PaymentAttempt.provider_namespace == provider_namespace,
                PaymentAttempt.provider_payment_reference == provider_payment_reference,
            ),
            for_update,
        )
    )


async def get_payment_settlement_for_attempt(
    session: AsyncSession, *, attempt_id: UUID, for_update: bool = False
) -> PaymentSettlement | None:
    return await session.scalar(
        _locked(
            select(PaymentSettlement).where(PaymentSettlement.payment_attempt_id == attempt_id), for_update
        )
    )


async def get_payment_settlement_for_order(
    session: AsyncSession, *, order_id: UUID, for_update: bool = False
) -> PaymentSettlement | None:
    return await session.scalar(
        _locked(select(PaymentSettlement).where(PaymentSettlement.order_id == order_id), for_update)
    )


async def get_payment_provider_event(
    session: AsyncSession,
    *,
    provider_namespace: str,
    external_event_id: str,
    for_update: bool = False,
) -> PaymentProviderEvent | None:
    return await session.scalar(
        _locked(
            select(PaymentProviderEvent).where(
                PaymentProviderEvent.provider_namespace == provider_namespace,
                PaymentProviderEvent.external_event_id == external_event_id,
            ),
            for_update,
        )
    )


async def get_payment_reconciliation_run(
    session: AsyncSession, *, run_key: str, for_update: bool = False
) -> PaymentReconciliationRun | None:
    """Fetch one durable reconciliation run by its caller-owned idempotency key."""

    return await session.scalar(
        _locked(
            select(PaymentReconciliationRun).where(PaymentReconciliationRun.run_key == run_key),
            for_update,
        )
    )


async def get_manual_destination(
    session: AsyncSession, destination_id: UUID, *, for_update: bool = False
) -> ManualPaymentDestination | None:
    return await session.scalar(
        _locked(
            select(ManualPaymentDestination).where(ManualPaymentDestination.id == destination_id), for_update
        )
    )


async def get_active_manual_destination(
    session: AsyncSession, *, for_update: bool = False
) -> ManualPaymentDestination | None:
    return await session.scalar(
        _locked(
            select(ManualPaymentDestination)
            .where(ManualPaymentDestination.status == ManualPaymentDestinationStatus.ACTIVE)
            .order_by(ManualPaymentDestination.approved_at.desc(), ManualPaymentDestination.id),
            for_update,
        )
    )


async def get_manual_proof(
    session: AsyncSession, *, proof_id: UUID, for_update: bool = False
) -> ManualPaymentProof | None:
    return await session.scalar(
        _locked(select(ManualPaymentProof).where(ManualPaymentProof.id == proof_id), for_update)
    )


async def get_manual_proof_for_attempt(
    session: AsyncSession, *, attempt_id: UUID, for_update: bool = False
) -> ManualPaymentProof | None:
    return await session.scalar(
        _locked(
            select(ManualPaymentProof)
            .where(ManualPaymentProof.payment_attempt_id == attempt_id)
            .order_by(ManualPaymentProof.created_at, ManualPaymentProof.id),
            for_update,
        )
    )


async def get_manual_proof_by_utr(
    session: AsyncSession, *, utr: str, for_update: bool = False
) -> ManualPaymentProof | None:
    return await session.scalar(
        _locked(select(ManualPaymentProof).where(ManualPaymentProof.utr == utr), for_update)
    )


async def list_manual_proofs_for_review(
    session: AsyncSession, *, limit: int
) -> list[ManualPaymentProof]:
    return list(
        await session.scalars(
            select(ManualPaymentProof)
            .where(ManualPaymentProof.status.in_(("SUBMITTED", "UNDER_REVIEW")))
            .order_by(ManualPaymentProof.submitted_at, ManualPaymentProof.id)
            .limit(limit)
        )
    )


async def get_payment_outbox_event(
    session: AsyncSession, *, event_id: UUID, for_update: bool = False
) -> PaymentOutboxEvent | None:
    return await session.scalar(
        _locked(select(PaymentOutboxEvent).where(PaymentOutboxEvent.id == event_id), for_update)
    )


async def list_dispatchable_payment_outbox_ids(
    session: AsyncSession, *, before: datetime, limit: int
) -> list[UUID]:
    return list(
        await session.scalars(
            select(PaymentOutboxEvent.id)
            .where(
                PaymentOutboxEvent.status.in_(
                    (PaymentOutboxEventStatus.PENDING, PaymentOutboxEventStatus.FAILED)
                ),
                (PaymentOutboxEvent.available_at.is_(None)) | (PaymentOutboxEvent.available_at <= before),
            )
            .order_by(PaymentOutboxEvent.created_at, PaymentOutboxEvent.id)
            .limit(limit)
        )
    )


async def list_payment_attempt_ids_due_for_expiry(
    session: AsyncSession, *, before: datetime, limit: int
) -> list[UUID]:
    return list(
        await session.scalars(
            select(PaymentAttempt.id)
            .where(
                PaymentAttempt.status == PaymentAttemptStatus.AWAITING_PAYMENT,
                PaymentAttempt.expires_at <= before,
            )
            .order_by(PaymentAttempt.expires_at, PaymentAttempt.id)
            .limit(limit)
        )
    )


async def list_payment_reconciliation_scopes(
    session: AsyncSession, *, limit: int
) -> list[tuple[PaymentMethod | None, str]]:
    """Return distinct payment-method/provider scopes with retained review work.

    This query intentionally only identifies work.  It does not claim events,
    call a provider, or alter an attempt/order state.  The service uses the
    returned scopes to write a durable review-queue snapshot.
    """

    if isinstance(limit, bool) or not isinstance(limit, int) or not 0 < limit <= 1_000:
        raise ValueError("limit must be an integer between 1 and 1000")

    rows = await session.execute(
        select(PaymentAttempt.method, PaymentAttempt.provider_namespace)
        .where(
            PaymentAttempt.status.in_(
                (PaymentAttemptStatus.UNDER_REVIEW, PaymentAttemptStatus.EXPIRED)
            )
        )
        .group_by(PaymentAttempt.method, PaymentAttempt.provider_namespace)
        .order_by(PaymentAttempt.method, PaymentAttempt.provider_namespace)
        .limit(limit)
    )
    scopes: list[tuple[PaymentMethod | None, str]] = [
        (method, namespace) for method, namespace in rows.all() if isinstance(namespace, str) and namespace
    ]

    # Signed callbacks can be retained for reconciliation before they can be
    # linked to an attempt.  Keep those namespaces visible to the worker too;
    # the service maps the two platform-owned namespaces explicitly and treats
    # every other namespace as the generic white-label boundary.
    remaining = limit - len(scopes)
    if remaining <= 0:
        return scopes
    event_rows = await session.scalars(
        select(PaymentProviderEvent.provider_namespace)
        .where(
            PaymentProviderEvent.status.in_(
                (PaymentProviderEventStatus.REVIEW_REQUIRED, PaymentProviderEventStatus.FAILED)
            )
        )
        .group_by(PaymentProviderEvent.provider_namespace)
        .order_by(PaymentProviderEvent.provider_namespace)
        .limit(remaining)
    )
    seen_namespaces = {namespace for _, namespace in scopes}
    for namespace in event_rows:
        if isinstance(namespace, str) and namespace and namespace not in seen_namespaces:
            scopes.append((None, namespace))
            seen_namespaces.add(namespace)
    return scopes
