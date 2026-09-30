"""Read/lock helpers for P2P withdrawal persistence; no lifecycle logic lives here."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.withdrawal import (
    PaymentDestination,
    PaymentDestinationStatus,
    WithdrawalRequest,
    WithdrawalStatus,
)


def _locked(statement, for_update: bool):
    return statement.with_for_update() if for_update else statement


async def get_withdrawal(
    session: AsyncSession, withdrawal_id: UUID, *, for_update: bool = False
) -> WithdrawalRequest | None:
    return await session.scalar(
        _locked(select(WithdrawalRequest).where(WithdrawalRequest.id == withdrawal_id), for_update)
    )


async def get_withdrawal_for_user(
    session: AsyncSession, *, withdrawal_id: UUID, user_id: UUID, for_update: bool = False
) -> WithdrawalRequest | None:
    return await session.scalar(
        _locked(
            select(WithdrawalRequest).where(
                WithdrawalRequest.id == withdrawal_id, WithdrawalRequest.user_id == user_id
            ),
            for_update,
        )
    )


async def get_unresolved_withdrawal_for_user(
    session: AsyncSession, *, user_id: UUID, for_update: bool = False
) -> WithdrawalRequest | None:
    return await session.scalar(
        _locked(
            select(WithdrawalRequest)
            .where(
                WithdrawalRequest.user_id == user_id,
                WithdrawalRequest.status.in_(
                    [
                        WithdrawalStatus.WAITING_FOR_BUYER,
                        WithdrawalStatus.MATCHED,
                        WithdrawalStatus.UNDER_REVIEW,
                    ]
                ),
            )
            .order_by(WithdrawalRequest.created_at, WithdrawalRequest.id),
            for_update,
        )
    )


async def get_payment_destination(
    session: AsyncSession, destination_id: UUID, *, for_update: bool = False
) -> PaymentDestination | None:
    return await session.scalar(
        _locked(select(PaymentDestination).where(PaymentDestination.id == destination_id), for_update)
    )


async def get_verified_destination_for_owner(
    session: AsyncSession, *, destination_id: UUID, user_id: UUID, for_update: bool = False
) -> PaymentDestination | None:
    return await session.scalar(
        _locked(
            select(PaymentDestination).where(
                PaymentDestination.id == destination_id,
                PaymentDestination.user_id == user_id,
                PaymentDestination.status == PaymentDestinationStatus.VERIFIED,
            ),
            for_update,
        )
    )


async def find_oldest_exact_withdrawal(
    session: AsyncSession,
    *,
    amount_paise: int,
    currency: str,
    excluding_user_id: UUID,
    for_update: bool = False,
) -> WithdrawalRequest | None:
    """Return the deterministic oldest exact, still-open candidate.

    The service must lock/validate its wallet hold and destination before it
    exposes a recipient. This query intentionally does not decide eligibility.
    """

    return await session.scalar(
        _locked(
            select(WithdrawalRequest)
            .where(
                WithdrawalRequest.status == WithdrawalStatus.WAITING_FOR_BUYER,
                WithdrawalRequest.amount_paise == amount_paise,
                WithdrawalRequest.currency == currency,
                WithdrawalRequest.user_id != excluding_user_id,
            )
            .order_by(WithdrawalRequest.created_at, WithdrawalRequest.id),
            for_update,
        )
    )
