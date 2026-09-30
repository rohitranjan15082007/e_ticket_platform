"""Read-only wallet and owner-scoped journal contracts."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.models.ledger import AccountKind, PostingDirection


class WalletResponse(BaseModel):
    id: UUID
    currency: str
    available_paise: int
    locked_paise: int
    total_paise: int
    version: int


class WalletTransactionResponse(BaseModel):
    id: UUID
    journal_group_id: UUID
    event_type: str
    account_kind: AccountKind
    direction: PostingDirection
    amount_paise: int
    currency: str
    created_at: datetime
