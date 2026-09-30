"""Owner-only wallet balance, provisioning and journal reads."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, status
from sqlalchemy import select

from app.dependencies import SessionDependency, get_current_user
from app.models.ledger import JournalGroup, JournalPosting, LedgerAccount
from app.models.user import User
from app.repositories.wallet_repository import get_wallet_by_user_id
from app.schemas.wallet import WalletResponse, WalletTransactionResponse
from app.services.wallet_service import WalletService


router = APIRouter(prefix="/wallet", tags=["wallet"])
CurrentUser = Annotated[User, Depends(get_current_user)]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=255)]


@router.get("", response_model=WalletResponse | None)
async def get_my_wallet(session: SessionDependency, current_user: CurrentUser) -> WalletResponse | None:
    wallet = await get_wallet_by_user_id(session, current_user.id)
    if wallet is None:
        return None
    return WalletResponse(
        id=wallet.id, currency=wallet.currency,
        available_paise=wallet.available_paise, locked_paise=wallet.locked_paise,
        total_paise=wallet.available_paise + wallet.locked_paise, version=wallet.version,
    )


@router.post("/provision", response_model=WalletResponse, status_code=status.HTTP_201_CREATED)
async def provision_my_wallet(
    session: SessionDependency, current_user: CurrentUser, idempotency_key: IdempotencyKey,
) -> WalletResponse:
    result = await WalletService(session).provision_wallet(
        user_id=current_user.id, actor_user_id=current_user.id, currency="INR",
        idempotency_key=idempotency_key, commit=True,
    )
    return WalletResponse.model_validate(result.response_payload["wallet"])


@router.get("/transactions", response_model=list[WalletTransactionResponse])
async def list_my_wallet_transactions(
    session: SessionDependency, current_user: CurrentUser,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[WalletTransactionResponse]:
    wallet = await get_wallet_by_user_id(session, current_user.id)
    if wallet is None:
        return []
    rows = (
        await session.execute(
            select(JournalPosting, LedgerAccount.kind, JournalGroup.event_type)
            .join(LedgerAccount, JournalPosting.account_id == LedgerAccount.id)
            .join(JournalGroup, JournalPosting.journal_group_id == JournalGroup.id)
            .where(LedgerAccount.wallet_id == wallet.id)
            .order_by(JournalPosting.created_at.desc(), JournalPosting.id.desc())
            .limit(limit)
        )
    ).all()
    return [
        WalletTransactionResponse(
            id=posting.id, journal_group_id=posting.journal_group_id,
            event_type=event_type, account_kind=kind, direction=posting.direction,
            amount_paise=posting.amount_paise, currency=posting.currency,
            created_at=posting.created_at,
        )
        for posting, kind, event_type in rows
    ]
