"""Double-entry journal creation and balance verification."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.money import require_currency, require_inr_currency, require_paise
from app.exceptions import InvariantViolationError
from app.models.ledger import (
    PLATFORM_CLEARING_ACCOUNT_CODE,
    PLATFORM_EMERGENCY_RESERVE_ACCOUNT_CODE,
    PLATFORM_EXTERNAL_ORDER_PENDING_ACCOUNT_CODE,
    PLATFORM_MARKETING_ACCOUNT_CODE,
    PLATFORM_OPERATIONS_ACCOUNT_CODE,
    PLATFORM_P2P_ORDER_PENDING_ACCOUNT_CODE,
    PLATFORM_PRIZE_POOL_ACCOUNT_CODE,
    PLATFORM_PROFIT_GROWTH_ACCOUNT_CODE,
    AccountKind,
    JournalGroup,
    JournalPosting,
    LedgerAccount,
    PostingDirection,
)
from app.models.wallet import Wallet


@dataclass(frozen=True, slots=True)
class PostingDraft:
    account_id: UUID
    direction: PostingDirection
    amount_paise: int
    currency: str


class LedgerService:
    """Creates journal groups only when debits equal credits in one currency."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def ensure_wallet_accounts(self, wallet: Wallet) -> dict[AccountKind, LedgerAccount]:
        """Provision the two liability accounts backing a new wallet."""

        require_inr_currency(wallet.currency)
        rows = await self.session.scalars(
            select(LedgerAccount)
            .where(LedgerAccount.wallet_id == wallet.id)
            .with_for_update()
        )
        accounts = {account.kind: account for account in rows}
        expected = {
            AccountKind.USER_AVAILABLE: f"wallet:{wallet.id}:available",
            AccountKind.USER_WITHDRAWAL_HELD: f"wallet:{wallet.id}:held",
        }
        for kind, code in expected.items():
            if kind not in accounts:
                account = LedgerAccount(
                    code=code,
                    kind=kind,
                    currency=wallet.currency,
                    wallet_id=wallet.id,
                    user_id=wallet.user_id,
                )
                self.session.add(account)
                accounts[kind] = account
        await self.session.flush()
        return accounts

    async def get_platform_clearing_account(self, currency: str) -> LedgerAccount:
        """Return the migration-provisioned INR clearing account.

        Creating this shared account lazily would make two first credits race on its
        unique code. It is instead seeded by the financial migration and treated as
        immutable accounting configuration.
        """

        currency = require_inr_currency(currency)
        account = await self.session.scalar(
            select(LedgerAccount).where(LedgerAccount.code == PLATFORM_CLEARING_ACCOUNT_CODE)
        )
        if account is None:
            raise InvariantViolationError(
                "The INR platform clearing account is missing; apply financial migrations first"
            )
        if account.kind != AccountKind.PLATFORM_CLEARING or account.currency != currency:
            raise InvariantViolationError("The configured platform clearing account is invalid")
        return account

    async def get_p2p_order_pending_account(self, currency: str) -> LedgerAccount:
        """Return the migration-provisioned order-funds account for direct P2P settlement.

        This account represents the buyer's already externally paid order funds
        pending product fulfillment.  It deliberately is not the platform
        clearing account: the buyer paid the receiver directly, so there is no
        platform cash receipt to record here.
        """

        currency = require_inr_currency(currency)
        account = await self.session.scalar(
            select(LedgerAccount).where(
                LedgerAccount.code == PLATFORM_P2P_ORDER_PENDING_ACCOUNT_CODE
            )
        )
        if account is None:
            raise InvariantViolationError(
                "The INR P2P order-funds account is missing; apply P2P migrations first"
            )
        if account.kind != AccountKind.PLATFORM_P2P_ORDER_PENDING or account.currency != currency:
            raise InvariantViolationError("The configured P2P order-funds account is invalid")
        return account

    async def get_external_order_pending_account(self, currency: str) -> LedgerAccount:
        """Return the migration-provisioned INR account for non-P2P external payments.

        This is separate from both wallet clearing and P2P pending funds. A
        provider-confirmed checkout has an external receipt trail and must not
        be represented as a direct buyer-to-receiver P2P transfer.
        """

        currency = require_inr_currency(currency)
        account = await self.session.scalar(
            select(LedgerAccount).where(
                LedgerAccount.code == PLATFORM_EXTERNAL_ORDER_PENDING_ACCOUNT_CODE
            )
        )
        if account is None:
            raise InvariantViolationError(
                "The INR external order-pending account is missing; apply Phase 5 migrations first"
            )
        if account.kind != AccountKind.PLATFORM_EXTERNAL_ORDER_PENDING or account.currency != currency:
            raise InvariantViolationError("The configured external order-pending account is invalid")
        return account

    async def get_revenue_bucket_account(
        self, *, kind: AccountKind, currency: str
    ) -> LedgerAccount:
        """Return one immutable platform revenue bucket seeded by Phase 7.

        The mapping is intentionally closed so a caller cannot route money to
        an arbitrary account merely by supplying a code.
        """

        configured = {
            AccountKind.PLATFORM_PRIZE_POOL: PLATFORM_PRIZE_POOL_ACCOUNT_CODE,
            AccountKind.PLATFORM_MARKETING: PLATFORM_MARKETING_ACCOUNT_CODE,
            AccountKind.PLATFORM_OPERATIONS: PLATFORM_OPERATIONS_ACCOUNT_CODE,
            AccountKind.PLATFORM_EMERGENCY_RESERVE: PLATFORM_EMERGENCY_RESERVE_ACCOUNT_CODE,
            AccountKind.PLATFORM_PROFIT_GROWTH: PLATFORM_PROFIT_GROWTH_ACCOUNT_CODE,
        }
        code = configured.get(kind)
        if code is None:
            raise InvariantViolationError("The requested ledger account is not a revenue bucket")
        normalized_currency = require_inr_currency(currency)
        account = await self.session.scalar(select(LedgerAccount).where(LedgerAccount.code == code))
        if account is None:
            raise InvariantViolationError(
                "A Phase 7 platform revenue account is missing; apply financial migrations first"
            )
        if account.kind != kind or account.currency != normalized_currency:
            raise InvariantViolationError("The configured platform revenue account is invalid")
        return account

    async def post_balanced_group(
        self,
        *,
        event_type: str,
        currency: str,
        reference_type: str,
        reference_id: UUID | None,
        actor_user_id: UUID | None,
        postings: list[PostingDraft],
        reason: str | None = None,
        journal_group_id: UUID | None = None,
        idempotency_record_id: UUID,
    ) -> JournalGroup:
        """Persist a balanced, immutable journal group in the caller's transaction."""

        normalized_currency = require_inr_currency(currency)
        self.validate_postings(postings, normalized_currency)
        await self._validate_posting_accounts(postings, normalized_currency)
        group = JournalGroup(
            **({"id": journal_group_id} if journal_group_id is not None else {}),
            event_type=event_type,
            currency=normalized_currency,
            reference_type=reference_type,
            reference_id=reference_id,
            idempotency_record_id=idempotency_record_id,
            actor_user_id=actor_user_id,
            reason=reason,
        )
        self.session.add(group)
        await self.session.flush()
        self.session.add_all(
            JournalPosting(
                journal_group_id=group.id,
                account_id=posting.account_id,
                direction=posting.direction,
                amount_paise=require_paise(posting.amount_paise, field="posting.amount_paise"),
                currency=normalized_currency,
            )
            for posting in postings
        )
        await self.session.flush()
        return group

    @staticmethod
    def validate_postings(postings: list[PostingDraft], currency: str) -> None:
        """Reject unbalanced, empty, cross-currency, or non-integer journal drafts."""

        normalized_currency = require_inr_currency(currency)
        if len(postings) < 2:
            raise InvariantViolationError("A journal group requires at least one debit and one credit")
        debit_total = 0
        credit_total = 0
        for posting in postings:
            amount = require_paise(posting.amount_paise, field="posting.amount_paise")
            if require_currency(posting.currency) != normalized_currency:
                raise InvariantViolationError("A journal group cannot mix currencies")
            if posting.direction == PostingDirection.DEBIT:
                debit_total += amount
            elif posting.direction == PostingDirection.CREDIT:
                credit_total += amount
            else:
                raise InvariantViolationError("A journal posting direction is invalid")
        if debit_total != credit_total:
            raise InvariantViolationError(
                f"Journal group is unbalanced: debits={debit_total}, credits={credit_total}"
            )

    async def assert_group_balanced(self, journal_group_id: UUID) -> None:
        """Re-check a persisted journal group for reconciliation and tests."""

        group = await self.session.get(JournalGroup, journal_group_id)
        if group is None:
            raise InvariantViolationError("Journal group does not exist")
        rows = await self.session.scalars(
            select(JournalPosting).where(JournalPosting.journal_group_id == journal_group_id)
        )
        drafts = [
            PostingDraft(
                account_id=row.account_id,
                direction=row.direction,
                amount_paise=row.amount_paise,
                currency=row.currency,
            )
            for row in rows
        ]
        self.validate_postings(drafts, group.currency)
        await self._validate_posting_accounts(drafts, group.currency)

    async def _validate_posting_accounts(
        self, postings: list[PostingDraft], currency: str
    ) -> None:
        """Require every persisted posting account to use the journal's currency."""

        account_ids = {posting.account_id for posting in postings}
        accounts = await self.session.scalars(
            select(LedgerAccount).where(LedgerAccount.id.in_(account_ids))
        )
        by_id = {account.id: account for account in accounts}
        if len(by_id) != len(account_ids):
            raise InvariantViolationError("A journal posting references an unknown ledger account")
        for account in by_id.values():
            if account.currency != currency:
                raise InvariantViolationError(
                    "A journal posting account must use the journal group's currency"
                )
