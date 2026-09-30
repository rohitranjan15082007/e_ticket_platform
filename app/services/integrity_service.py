"""Read-only ledger integrity scan for operators and alerting.

This never repairs balances or journals. A finding requires investigation and
an explicit reversal/correction workflow, not a silent database update.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ledger import AccountKind, JournalGroup, JournalPosting, LedgerAccount, PostingDirection
from app.models.wallet import Wallet


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    journal_groups_checked: int
    unbalanced_group_count: int
    unbalanced_group_ids: tuple[UUID, ...]
    wallets_checked: int
    wallet_mismatch_count: int
    wallet_mismatch_ids: tuple[UUID, ...]
    findings_truncated: bool

    @property
    def healthy(self) -> bool:
        return self.unbalanced_group_count == 0 and self.wallet_mismatch_count == 0


class IntegrityService:
    """Compare persisted postings and wallet caches without mutating state."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def scan(self, *, include_wallets: bool = True, max_findings: int = 100) -> IntegrityReport:
        if not 1 <= max_findings <= 1000:
            raise ValueError("max_findings must be between 1 and 1000")
        group_count = 0
        bad_group_count = 0
        bad_groups: list[UUID] = []
        debit_total = func.coalesce(func.sum(case(
            (JournalPosting.direction == PostingDirection.DEBIT, JournalPosting.amount_paise),
            else_=0,
        )), 0)
        credit_total = func.coalesce(func.sum(case(
            (JournalPosting.direction == PostingDirection.CREDIT, JournalPosting.amount_paise),
            else_=0,
        )), 0)
        currency_errors = func.coalesce(func.sum(case(
            (JournalPosting.currency != JournalGroup.currency, 1),
            (LedgerAccount.currency != JournalGroup.currency, 1),
            else_=0,
        )), 0)
        group_query = (
            select(
                JournalGroup.id,
                func.count(JournalPosting.id),
                debit_total,
                credit_total,
                currency_errors,
            )
            .outerjoin(JournalPosting, JournalPosting.journal_group_id == JournalGroup.id)
            .outerjoin(LedgerAccount, LedgerAccount.id == JournalPosting.account_id)
            .group_by(JournalGroup.id)
            .order_by(JournalGroup.id)
        )
        groups = await self.session.stream(group_query)
        async for group_id, posting_count, debits, credits, bad_currency_count in groups:
            group_count += 1
            if posting_count < 2 or debits == 0 or credits == 0 or debits != credits or bad_currency_count:
                bad_group_count += 1
                if len(bad_groups) < max_findings:
                    bad_groups.append(group_id)

        wallet_count = 0
        bad_wallet_count = 0
        bad_wallets: list[UUID] = []
        if include_wallets:
            signed_amount = case(
                (JournalPosting.direction == PostingDirection.CREDIT, JournalPosting.amount_paise),
                (JournalPosting.direction == PostingDirection.DEBIT, -JournalPosting.amount_paise),
                else_=0,
            )
            available_from_postings = func.coalesce(func.sum(case(
                (LedgerAccount.kind == AccountKind.USER_AVAILABLE, signed_amount),
                else_=0,
            )), 0)
            held_from_postings = func.coalesce(func.sum(case(
                (LedgerAccount.kind == AccountKind.USER_WITHDRAWAL_HELD, signed_amount),
                else_=0,
            )), 0)
            wallet_query = (
                select(
                    Wallet.id, Wallet.available_paise, Wallet.locked_paise,
                    available_from_postings, held_from_postings,
                )
                .outerjoin(
                    LedgerAccount,
                    and_(
                        LedgerAccount.wallet_id == Wallet.id,
                        LedgerAccount.kind.in_(
                            (AccountKind.USER_AVAILABLE, AccountKind.USER_WITHDRAWAL_HELD)
                        ),
                    ),
                )
                .outerjoin(JournalPosting, JournalPosting.account_id == LedgerAccount.id)
                .group_by(Wallet.id)
                .order_by(Wallet.id)
            )
            wallets = await self.session.stream(wallet_query)
            async for wallet_id, available, held, posted_available, posted_held in wallets:
                wallet_count += 1
                if available != posted_available or held != posted_held:
                    bad_wallet_count += 1
                    if len(bad_wallets) < max_findings:
                        bad_wallets.append(wallet_id)

        return IntegrityReport(
            journal_groups_checked=group_count,
            unbalanced_group_count=bad_group_count,
            unbalanced_group_ids=tuple(bad_groups),
            wallets_checked=wallet_count,
            wallet_mismatch_count=bad_wallet_count,
            wallet_mismatch_ids=tuple(bad_wallets),
            findings_truncated=bad_group_count > len(bad_groups) or bad_wallet_count > len(bad_wallets),
        )
