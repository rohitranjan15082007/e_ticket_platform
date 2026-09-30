"""Read/lock helpers for P2P match, evidence and settlement persistence."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.p2p_match import (
    ACTIVE_MATCH_STATUSES,
    P2PDispute,
    P2PMatch,
    P2PPaymentSubmission,
    P2PReceiverConfirmation,
    P2PRefund,
    P2PSettlement,
    P2PVerifiedPaymentReference,
)


def _locked(statement, for_update: bool):
    return statement.with_for_update() if for_update else statement


async def get_match(session: AsyncSession, match_id: UUID, *, for_update: bool = False) -> P2PMatch | None:
    return await session.scalar(_locked(select(P2PMatch).where(P2PMatch.id == match_id), for_update))


async def get_active_match_for_order(
    session: AsyncSession, order_id: UUID, *, for_update: bool = False
) -> P2PMatch | None:
    return await session.scalar(
        _locked(
            select(P2PMatch)
            .where(P2PMatch.order_id == order_id, P2PMatch.status.in_(ACTIVE_MATCH_STATUSES))
            .order_by(P2PMatch.created_at, P2PMatch.id),
            for_update,
        )
    )


async def get_active_match_for_withdrawal(
    session: AsyncSession, withdrawal_id: UUID, *, for_update: bool = False
) -> P2PMatch | None:
    return await session.scalar(
        _locked(
            select(P2PMatch)
            .where(P2PMatch.withdrawal_id == withdrawal_id, P2PMatch.status.in_(ACTIVE_MATCH_STATUSES))
            .order_by(P2PMatch.created_at, P2PMatch.id),
            for_update,
        )
    )


async def get_submission(
    session: AsyncSession, submission_id: UUID, *, for_update: bool = False
) -> P2PPaymentSubmission | None:
    return await session.scalar(
        _locked(select(P2PPaymentSubmission).where(P2PPaymentSubmission.id == submission_id), for_update)
    )


async def get_submission_for_match_reference(
    session: AsyncSession,
    *,
    match_id: UUID,
    provider_namespace: str,
    claimed_reference: str,
    for_update: bool = False,
) -> P2PPaymentSubmission | None:
    return await session.scalar(
        _locked(
            select(P2PPaymentSubmission).where(
                P2PPaymentSubmission.match_id == match_id,
                P2PPaymentSubmission.provider_namespace == provider_namespace,
                P2PPaymentSubmission.claimed_reference == claimed_reference,
            ),
            for_update,
        )
    )


async def list_other_submissions_for_reference(
    session: AsyncSession,
    *,
    match_id: UUID,
    provider_namespace: str,
    claimed_reference: str,
    for_update: bool = False,
) -> list[P2PPaymentSubmission]:
    return list(
        await session.scalars(
            _locked(
                select(P2PPaymentSubmission)
                .where(
                    P2PPaymentSubmission.match_id != match_id,
                    P2PPaymentSubmission.provider_namespace == provider_namespace,
                    P2PPaymentSubmission.claimed_reference == claimed_reference,
                )
                .order_by(P2PPaymentSubmission.created_at, P2PPaymentSubmission.id),
                for_update,
            )
        )
    )


async def get_receiver_confirmation(
    session: AsyncSession, match_id: UUID, *, for_update: bool = False
) -> P2PReceiverConfirmation | None:
    return await session.scalar(
        _locked(
            select(P2PReceiverConfirmation).where(P2PReceiverConfirmation.match_id == match_id), for_update
        )
    )


async def get_dispute(session: AsyncSession, dispute_id: UUID, *, for_update: bool = False) -> P2PDispute | None:
    return await session.scalar(_locked(select(P2PDispute).where(P2PDispute.id == dispute_id), for_update))


async def get_dispute_for_match(
    session: AsyncSession, match_id: UUID, *, for_update: bool = False
) -> P2PDispute | None:
    return await session.scalar(
        _locked(select(P2PDispute).where(P2PDispute.match_id == match_id), for_update)
    )


async def get_settlement_for_match(
    session: AsyncSession, match_id: UUID, *, for_update: bool = False
) -> P2PSettlement | None:
    return await session.scalar(
        _locked(select(P2PSettlement).where(P2PSettlement.match_id == match_id), for_update)
    )


async def get_verified_reference(
    session: AsyncSession,
    *,
    provider_namespace: str,
    transaction_reference: str,
    for_update: bool = False,
) -> P2PVerifiedPaymentReference | None:
    return await session.scalar(
        _locked(
            select(P2PVerifiedPaymentReference).where(
                P2PVerifiedPaymentReference.provider_namespace == provider_namespace,
                P2PVerifiedPaymentReference.transaction_reference == transaction_reference,
            ),
            for_update,
        )
    )


async def get_verified_reference_for_match(
    session: AsyncSession, match_id: UUID, *, for_update: bool = False
) -> P2PVerifiedPaymentReference | None:
    return await session.scalar(
        _locked(
            select(P2PVerifiedPaymentReference)
            .where(P2PVerifiedPaymentReference.match_id == match_id)
            .order_by(P2PVerifiedPaymentReference.created_at, P2PVerifiedPaymentReference.id),
            for_update,
        )
    )


async def get_refund_for_match(
    session: AsyncSession, match_id: UUID, *, for_update: bool = False
) -> P2PRefund | None:
    return await session.scalar(
        _locked(select(P2PRefund).where(P2PRefund.match_id == match_id), for_update)
    )


async def list_matches_due_for_payment_expiry(
    session: AsyncSession, *, before: datetime, limit: int
) -> list[UUID]:
    return list(
        await session.scalars(
            select(P2PMatch.id)
            .where(P2PMatch.payment_deadline_at <= before, P2PMatch.status == "WAITING_FOR_PAYMENT")
            .order_by(P2PMatch.payment_deadline_at, P2PMatch.id)
            .limit(limit)
        )
    )


async def list_matches_due_for_receiver_expiry(
    session: AsyncSession, *, before: datetime, limit: int
) -> list[UUID]:
    return list(
        await session.scalars(
            select(P2PMatch.id)
            .where(
                P2PMatch.receiver_confirmation_deadline_at.is_not(None),
                P2PMatch.receiver_confirmation_deadline_at <= before,
                P2PMatch.status == "WAITING_FOR_RECEIVER_CONFIRMATION",
            )
            .order_by(P2PMatch.receiver_confirmation_deadline_at, P2PMatch.id)
            .limit(limit)
        )
    )
