"""Transactional wallet mutations backed by named holds, balanced journals and audits."""

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.audit import AuditService
from app.core.idempotency import IdempotencyService, canonical_payload, fingerprint
from app.core.money import require_inr_currency, require_paise
from app.exceptions import (
    AuthorizationError,
    ConflictError,
    InsufficientFundsError,
    ValidationError,
)
from app.models.ledger import AccountKind, JournalGroup, PostingDirection
from app.models.user import IdempotencyRecord, User
from app.models.wallet import Wallet, WalletHold, WalletHoldStatus
from app.repositories.wallet_repository import (
    get_wallet_by_id,
    get_wallet_by_user_id,
    get_wallet_hold_by_reference,
)
from app.services.ledger_service import LedgerService, PostingDraft


@dataclass(slots=True)
class WalletMutationResult:
    """Result plus the persisted, immutable response snapshot for retry callers."""

    wallet: "WalletSnapshot"
    journal_group: JournalGroup | None
    wallet_hold: "WalletHoldSnapshot | None"
    response_payload: dict[str, object]
    replayed: bool


@dataclass(frozen=True, slots=True)
class WalletSnapshot:
    """The exact wallet state returned by one completed mutation."""

    id: UUID
    currency: str
    available_paise: int
    locked_paise: int
    total_paise: int
    version: int


@dataclass(frozen=True, slots=True)
class WalletHoldSnapshot:
    """The exact named-hold state returned by one completed mutation."""

    id: UUID
    wallet_id: UUID
    reference_type: str
    reference_id: UUID
    amount_paise: int
    currency: str
    status: WalletHoldStatus
    hold_journal_group_id: UUID
    release_journal_group_id: UUID | None
    settlement_journal_group_id: UUID | None


@dataclass(frozen=True, slots=True)
class WalletHoldSettlementResult:
    """Internal P2P settlement result composed into one outer P2P transaction."""

    wallet: "WalletSnapshot"
    journal_group: JournalGroup
    wallet_hold: "WalletHoldSnapshot"


class WalletService:
    """Apply wallet state changes under locks without hidden transaction commits.

    Each public mutation requires an explicit ``commit`` choice. A top-level use
    case may choose ``commit=True``; a composed use case must pass ``False`` and
    commit or roll back its full unit of work itself.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.ledger = LedgerService(session)
        self.audit = AuditService(session)
        self.idempotency = IdempotencyService(session)

    async def provision_wallet(
        self,
        *,
        user_id: UUID,
        actor_user_id: UUID,
        currency: str,
        idempotency_key: str,
        commit: bool,
    ) -> WalletMutationResult:
        """Create one INR wallet and its backing accounts for its owner or an admin."""

        currency = require_inr_currency(currency)
        payload = {"user_id": user_id, "currency": currency}
        scope = f"user:{actor_user_id}:wallet.provision"
        request_fingerprint = fingerprint(payload)
        user = await self._locked_user(user_id)
        await self._assert_provision_actor(actor_user_id, user)
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay_wallet(replay)
        if await get_wallet_by_user_id(self.session, user_id, for_update=True) is not None:
            raise ConflictError("WALLET_ALREADY_EXISTS", "This user already has a wallet")

        wallet = Wallet(id=uuid4(), user_id=user_id, currency=currency)
        self.session.add(wallet)
        await self.session.flush()
        await self.ledger.ensure_wallet_accounts(wallet)
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="wallet",
            entity_id=wallet.id,
            action="WALLET_CREATED",
            before_state=None,
            after_state=self._wallet_state(wallet),
        )
        response_payload = self._response_payload(wallet=wallet, group=None, hold=None)
        self.idempotency.record(
            actor_scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint,
            resource_type="wallet",
            resource_id=wallet.id,
            status_code=201,
            response_payload=response_payload,
        )
        await self._finish(commit=commit)
        return WalletMutationResult(
            wallet=self._wallet_snapshot(wallet),
            journal_group=None,
            wallet_hold=None,
            response_payload=response_payload,
            replayed=False,
        )

    async def credit_available(
        self,
        *,
        wallet_id: UUID,
        actor_user_id: UUID | None,
        amount_paise: int,
        idempotency_key: str,
        source_type: str,
        source_id: UUID,
        reason: str,
        commit: bool,
    ) -> WalletMutationResult:
        """Record a host-accounting credit; this service is intentionally not an API route.

        A user cannot act as the source: a non-system actor must be an administrator.
        The host source's stable type and UUID are journalled so a later accounting
        integration can reconcile the origin without inventing provider verification.
        """

        amount = require_paise(amount_paise)
        source_type = self._require_text(source_type, field="source_type", max_length=100)
        source_id = self._require_uuid(source_id, field="source_id")
        reason = self._require_text(reason, field="reason", max_length=500)
        payload = {
            "wallet_id": wallet_id,
            "amount_paise": amount,
            "source_type": source_type,
            "source_id": source_id,
            "reason": reason,
        }
        scope = "system:wallet.credit" if actor_user_id is None else f"admin:{actor_user_id}:wallet.credit"
        request_fingerprint = fingerprint(payload)
        wallet = await self._locked_wallet(wallet_id)
        await self._assert_credit_actor(actor_user_id)
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay_mutation(wallet, replay)

        accounts = await self.ledger.ensure_wallet_accounts(wallet)
        clearing = await self.ledger.get_platform_clearing_account(wallet.currency)
        before_state = self._wallet_state(wallet)
        record, group_id = await self._begin_group_idempotency(
            scope=scope,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        group = await self.ledger.post_balanced_group(
            journal_group_id=group_id,
            idempotency_record_id=record.id,
            event_type="WALLET_CREDITED",
            currency=wallet.currency,
            reference_type=source_type,
            reference_id=source_id,
            actor_user_id=actor_user_id,
            reason=reason,
            postings=[
                PostingDraft(clearing.id, PostingDirection.DEBIT, amount, wallet.currency),
                PostingDraft(
                    accounts[AccountKind.USER_AVAILABLE].id,
                    PostingDirection.CREDIT,
                    amount,
                    wallet.currency,
                ),
            ],
        )
        wallet.available_paise += amount
        wallet.version += 1
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="wallet",
            entity_id=wallet.id,
            action="WALLET_CREDITED",
            before_state=before_state,
            after_state=self._wallet_state(wallet),
            reason=reason,
        )
        response_payload = self._response_payload(wallet=wallet, group=group, hold=None)
        record.response_payload = response_payload
        await self._finish(commit=commit)
        return WalletMutationResult(self._wallet_snapshot(wallet), group, None, response_payload, False)

    async def hold_available(
        self,
        *,
        wallet_id: UUID,
        actor_user_id: UUID,
        amount_paise: int,
        idempotency_key: str,
        reference_type: str,
        reference_id: UUID,
        reason: str,
        commit: bool,
    ) -> WalletMutationResult:
        """Create exactly one named hold and move its money into the held account."""

        amount = require_paise(amount_paise)
        reference_type = self._require_text(reference_type, field="reference_type", max_length=100)
        reference_id = self._require_uuid(reference_id, field="reference_id")
        reason = self._require_text(reason, field="reason", max_length=500)
        payload = {
            "wallet_id": wallet_id,
            "amount_paise": amount,
            "reference_type": reference_type,
            "reference_id": reference_id,
            "reason": reason,
        }
        scope = f"user:{actor_user_id}:wallet.hold"
        request_fingerprint = fingerprint(payload)
        wallet = await self._locked_wallet(wallet_id)
        await self._assert_wallet_actor(wallet, actor_user_id)
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay_mutation(wallet, replay)
        existing_hold = await get_wallet_hold_by_reference(
            self.session,
            wallet_id=wallet.id,
            reference_type=reference_type,
            reference_id=reference_id,
            for_update=True,
        )
        if existing_hold is not None:
            raise ConflictError("WALLET_HOLD_ALREADY_EXISTS", "This reference already owns a wallet hold")
        if wallet.available_paise < amount:
            raise InsufficientFundsError()

        accounts = await self.ledger.ensure_wallet_accounts(wallet)
        before_state = self._wallet_state(wallet)
        record, group_id = await self._begin_group_idempotency(
            scope=scope,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        group = await self.ledger.post_balanced_group(
            journal_group_id=group_id,
            idempotency_record_id=record.id,
            event_type="WALLET_FUNDS_HELD",
            currency=wallet.currency,
            reference_type=reference_type,
            reference_id=reference_id,
            actor_user_id=actor_user_id,
            reason=reason,
            postings=[
                PostingDraft(
                    accounts[AccountKind.USER_AVAILABLE].id,
                    PostingDirection.DEBIT,
                    amount,
                    wallet.currency,
                ),
                PostingDraft(
                    accounts[AccountKind.USER_WITHDRAWAL_HELD].id,
                    PostingDirection.CREDIT,
                    amount,
                    wallet.currency,
                ),
            ],
        )
        hold = WalletHold(
            id=uuid4(),
            wallet_id=wallet.id,
            reference_type=reference_type,
            reference_id=reference_id,
            amount_paise=amount,
            currency=wallet.currency,
            status=WalletHoldStatus.ACTIVE,
            hold_journal_group_id=group.id,
        )
        self.session.add(hold)
        wallet.available_paise -= amount
        wallet.locked_paise += amount
        wallet.version += 1
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="wallet",
            entity_id=wallet.id,
            action="WALLET_FUNDS_HELD",
            before_state=before_state,
            after_state=self._wallet_state(wallet),
            reason=reason,
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="wallet_hold",
            entity_id=hold.id,
            action="WALLET_HOLD_CREATED",
            before_state=None,
            after_state=self._hold_state(hold),
            reason=reason,
        )
        response_payload = self._response_payload(wallet=wallet, group=group, hold=hold)
        record.response_payload = response_payload
        await self._finish(commit=commit)
        return WalletMutationResult(
            self._wallet_snapshot(wallet),
            group,
            self._hold_snapshot(hold),
            response_payload,
            False,
        )

    async def release_locked(
        self,
        *,
        wallet_id: UUID,
        actor_user_id: UUID,
        amount_paise: int,
        idempotency_key: str,
        reference_type: str,
        reference_id: UUID,
        reason: str,
        commit: bool,
    ) -> WalletMutationResult:
        """Reverse one specific active hold; arbitrary or partial releases are rejected."""

        amount = require_paise(amount_paise)
        reference_type = self._require_text(reference_type, field="reference_type", max_length=100)
        reference_id = self._require_uuid(reference_id, field="reference_id")
        reason = self._require_text(reason, field="reason", max_length=500)
        payload = {
            "wallet_id": wallet_id,
            "amount_paise": amount,
            "reference_type": reference_type,
            "reference_id": reference_id,
            "reason": reason,
        }
        scope = f"user:{actor_user_id}:wallet.release"
        request_fingerprint = fingerprint(payload)
        wallet = await self._locked_wallet(wallet_id)
        await self._assert_wallet_actor(wallet, actor_user_id)
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay_mutation(wallet, replay)
        hold = await get_wallet_hold_by_reference(
            self.session,
            wallet_id=wallet.id,
            reference_type=reference_type,
            reference_id=reference_id,
            for_update=True,
        )
        if hold is None:
            raise ConflictError("WALLET_HOLD_NOT_FOUND", "No active wallet hold exists for this reference")
        if hold.status != WalletHoldStatus.ACTIVE:
            raise ConflictError("WALLET_HOLD_NOT_ACTIVE", "This wallet hold has already been resolved")
        if hold.amount_paise != amount:
            raise ConflictError("WALLET_HOLD_AMOUNT_MISMATCH", "A hold must be released in its exact amount")
        if wallet.locked_paise < amount:
            raise InsufficientFundsError("Locked wallet balance is inconsistent with the active hold")

        accounts = await self.ledger.ensure_wallet_accounts(wallet)
        before_state = self._wallet_state(wallet)
        before_hold_state = self._hold_state(hold)
        record, group_id = await self._begin_group_idempotency(
            scope=scope,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        group = await self.ledger.post_balanced_group(
            journal_group_id=group_id,
            idempotency_record_id=record.id,
            event_type="WALLET_HOLD_RELEASED",
            currency=wallet.currency,
            reference_type=reference_type,
            reference_id=reference_id,
            actor_user_id=actor_user_id,
            reason=reason,
            postings=[
                PostingDraft(
                    accounts[AccountKind.USER_WITHDRAWAL_HELD].id,
                    PostingDirection.DEBIT,
                    amount,
                    wallet.currency,
                ),
                PostingDraft(
                    accounts[AccountKind.USER_AVAILABLE].id,
                    PostingDirection.CREDIT,
                    amount,
                    wallet.currency,
                ),
            ],
        )
        hold.status = WalletHoldStatus.RELEASED
        hold.release_journal_group_id = group.id
        hold.released_at = datetime.now(timezone.utc)
        wallet.locked_paise -= amount
        wallet.available_paise += amount
        wallet.version += 1
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="wallet",
            entity_id=wallet.id,
            action="WALLET_HOLD_RELEASED",
            before_state=before_state,
            after_state=self._wallet_state(wallet),
            reason=reason,
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="wallet_hold",
            entity_id=hold.id,
            action="WALLET_HOLD_RELEASED",
            before_state=before_hold_state,
            after_state=self._hold_state(hold),
            reason=reason,
        )
        response_payload = self._response_payload(wallet=wallet, group=group, hold=hold)
        record.response_payload = response_payload
        await self._finish(commit=commit)
        return WalletMutationResult(
            self._wallet_snapshot(wallet),
            group,
            self._hold_snapshot(hold),
            response_payload,
            False,
        )

    async def settle_active_hold_for_p2p(
        self,
        *,
        wallet_id: UUID,
        amount_paise: int,
        reference_type: str,
        reference_id: UUID,
        settlement_id: UUID,
        idempotency_record_id: UUID,
        actor_user_id: UUID | None,
        reason: str,
        commit: bool,
    ) -> WalletHoldSettlementResult:
        """Consume one active P2P hold after the P2P workflow has authorized settlement.

        This is intentionally an internal composition primitive, not an API
        operation.  Its caller owns the P2P state/evidence validation and has
        already created the action-scoped idempotency record that this journal
        group references.  Unlike ``release_locked``, it never returns money to
        the receiver's available wallet balance.
        """

        amount = require_paise(amount_paise)
        reference_type = self._require_text(reference_type, field="reference_type", max_length=100)
        reference_id = self._require_uuid(reference_id, field="reference_id")
        settlement_id = self._require_uuid(settlement_id, field="settlement_id")
        idempotency_record_id = self._require_uuid(
            idempotency_record_id, field="idempotency_record_id"
        )
        reason = self._require_text(reason, field="reason", max_length=500)
        wallet = await self._locked_wallet(wallet_id)
        hold = await get_wallet_hold_by_reference(
            self.session,
            wallet_id=wallet.id,
            reference_type=reference_type,
            reference_id=reference_id,
            for_update=True,
        )
        if hold is None:
            raise ConflictError("WALLET_HOLD_NOT_FOUND", "No active wallet hold exists for this reference")
        if hold.status != WalletHoldStatus.ACTIVE:
            raise ConflictError("WALLET_HOLD_NOT_ACTIVE", "This wallet hold has already been resolved")
        if hold.amount_paise != amount:
            raise ConflictError("WALLET_HOLD_AMOUNT_MISMATCH", "A hold must settle in its exact amount")
        if wallet.locked_paise < amount:
            raise InsufficientFundsError("Locked wallet balance is inconsistent with the active hold")

        accounts = await self.ledger.ensure_wallet_accounts(wallet)
        pending_fulfillment = await self.ledger.get_p2p_order_pending_account(wallet.currency)
        before_state = self._wallet_state(wallet)
        before_hold_state = self._hold_state(hold)
        group = await self.ledger.post_balanced_group(
            event_type="P2P_WITHDRAWAL_SETTLED",
            currency=wallet.currency,
            reference_type="p2p_settlement",
            reference_id=settlement_id,
            actor_user_id=actor_user_id,
            reason=reason,
            postings=[
                PostingDraft(
                    accounts[AccountKind.USER_WITHDRAWAL_HELD].id,
                    PostingDirection.DEBIT,
                    amount,
                    wallet.currency,
                ),
                PostingDraft(
                    pending_fulfillment.id,
                    PostingDirection.CREDIT,
                    amount,
                    wallet.currency,
                ),
            ],
            idempotency_record_id=idempotency_record_id,
        )
        hold.status = WalletHoldStatus.SETTLED
        hold.settlement_journal_group_id = group.id
        hold.settled_at = datetime.now(timezone.utc)
        wallet.locked_paise -= amount
        wallet.version += 1
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="wallet",
            entity_id=wallet.id,
            action="P2P_WALLET_HOLD_SETTLED",
            before_state=before_state,
            after_state=self._wallet_state(wallet),
            reason=reason,
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="wallet_hold",
            entity_id=hold.id,
            action="P2P_WALLET_HOLD_SETTLED",
            before_state=before_hold_state,
            after_state=self._hold_state(hold),
            reason=reason,
        )
        await self._finish(commit=commit)
        return WalletHoldSettlementResult(
            wallet=self._wallet_snapshot(wallet),
            journal_group=group,
            wallet_hold=self._hold_snapshot(hold),
        )

    async def _begin_group_idempotency(
        self,
        *,
        scope: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> tuple[IdempotencyRecord, UUID]:
        """Insert the key before the journal so the group can hold the immutable link."""

        group_id = uuid4()
        record = self.idempotency.record(
            actor_scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint,
            resource_type="journal_group",
            resource_id=group_id,
            record_id=uuid4(),
        )
        await self.session.flush()
        return record, group_id

    async def _finish(self, *, commit: bool) -> None:
        """Flush every change; the caller explicitly decides whether to commit it."""

        await self.session.flush()
        if commit:
            await self.session.commit()

    async def _locked_user(self, user_id: UUID) -> User:
        user = await self.session.scalar(
            select(User).options(selectinload(User.roles)).where(User.id == user_id).with_for_update()
        )
        if user is None:
            raise ValidationError("UNKNOWN_USER", "Cannot provision a wallet for an unknown user")
        return user

    async def _locked_wallet(self, wallet_id: UUID) -> Wallet:
        wallet = await get_wallet_by_id(self.session, wallet_id, for_update=True)
        if wallet is None:
            raise ValidationError("UNKNOWN_WALLET", "Wallet does not exist")
        return wallet

    async def _assert_provision_actor(self, actor_user_id: UUID, target_user: User) -> None:
        if actor_user_id == target_user.id and target_user.is_active:
            return
        await self._require_admin(actor_user_id)

    async def _assert_wallet_actor(self, wallet: Wallet, actor_user_id: UUID) -> None:
        if actor_user_id == wallet.user_id:
            actor = await self._get_active_actor(actor_user_id)
            if actor is not None:
                return
        await self._require_admin(actor_user_id)

    async def _assert_credit_actor(self, actor_user_id: UUID | None) -> None:
        if actor_user_id is None:
            return
        await self._require_admin(actor_user_id)

    async def _get_active_actor(self, actor_user_id: UUID) -> User | None:
        actor = await self.session.scalar(
            select(User).options(selectinload(User.roles)).where(User.id == actor_user_id)
        )
        if actor is None or not actor.is_active:
            return None
        return actor

    async def _require_admin(self, actor_user_id: UUID) -> User:
        actor = await self._get_active_actor(actor_user_id)
        if actor is None or "admin" not in {role.name for role in actor.roles}:
            raise AuthorizationError("This financial operation requires the wallet owner or an administrator")
        return actor

    async def _replay_wallet(self, record: IdempotencyRecord) -> WalletMutationResult:
        if record.resource_id is None:
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior mutation has no wallet resource")
        wallet = await get_wallet_by_id(self.session, record.resource_id)
        if wallet is None:
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior wallet cannot be recovered")
        return WalletMutationResult(
            wallet=self._wallet_snapshot_from_response(self._stored_response(record)),
            journal_group=None,
            wallet_hold=None,
            response_payload=self._stored_response(record),
            replayed=True,
        )

    async def _replay_mutation(
        self, wallet: Wallet, record: IdempotencyRecord
    ) -> WalletMutationResult:
        if record.resource_id is None:
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior mutation has no journal resource")
        group = await self.session.get(JournalGroup, record.resource_id)
        if group is None:
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior journal cannot be recovered")
        response_payload = self._stored_response(record)
        hold = self._hold_snapshot_from_response(response_payload)
        if hold is not None and await self.session.get(WalletHold, hold.id) is None:
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior hold cannot be recovered")
        return WalletMutationResult(
            self._wallet_snapshot_from_response(response_payload),
            group,
            hold,
            response_payload,
            True,
        )

    @staticmethod
    def _stored_response(record: IdempotencyRecord) -> dict[str, object]:
        if not isinstance(record.response_payload, dict):
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior mutation has no stored response")
        return dict(record.response_payload)

    @staticmethod
    def _require_text(value: object, *, field: str, max_length: int) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError("INVALID_REFERENCE", f"{field} must be a non-empty string")
        normalized = value.strip()
        if len(normalized) > max_length:
            raise ValidationError("INVALID_REFERENCE", f"{field} must be at most {max_length} characters")
        return normalized

    @staticmethod
    def _require_uuid(value: object, *, field: str) -> UUID:
        if not isinstance(value, UUID):
            raise ValidationError("INVALID_REFERENCE", f"{field} must be a UUID")
        return value

    @staticmethod
    def _wallet_state(wallet: Wallet) -> dict[str, object]:
        return {
            "id": wallet.id,
            "currency": wallet.currency,
            "available_paise": wallet.available_paise,
            "locked_paise": wallet.locked_paise,
            "total_paise": wallet.available_paise + wallet.locked_paise,
            "version": wallet.version,
        }

    @staticmethod
    def _hold_state(hold: WalletHold) -> dict[str, object]:
        return {
            "id": hold.id,
            "wallet_id": hold.wallet_id,
            "reference_type": hold.reference_type,
            "reference_id": hold.reference_id,
            "amount_paise": hold.amount_paise,
            "currency": hold.currency,
            "status": hold.status,
            "hold_journal_group_id": hold.hold_journal_group_id,
            "release_journal_group_id": hold.release_journal_group_id,
            "settlement_journal_group_id": hold.settlement_journal_group_id,
        }

    @staticmethod
    def _wallet_snapshot(wallet: Wallet) -> WalletSnapshot:
        return WalletSnapshot(
            id=wallet.id,
            currency=wallet.currency,
            available_paise=wallet.available_paise,
            locked_paise=wallet.locked_paise,
            total_paise=wallet.available_paise + wallet.locked_paise,
            version=wallet.version,
        )

    @staticmethod
    def _hold_snapshot(hold: WalletHold) -> WalletHoldSnapshot:
        return WalletHoldSnapshot(
            id=hold.id,
            wallet_id=hold.wallet_id,
            reference_type=hold.reference_type,
            reference_id=hold.reference_id,
            amount_paise=hold.amount_paise,
            currency=hold.currency,
            status=hold.status,
            hold_journal_group_id=hold.hold_journal_group_id,
            release_journal_group_id=hold.release_journal_group_id,
            settlement_journal_group_id=hold.settlement_journal_group_id,
        )

    @classmethod
    def _wallet_snapshot_from_response(cls, response_payload: dict[str, object]) -> WalletSnapshot:
        data = response_payload.get("wallet")
        if not isinstance(data, dict):
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior wallet response cannot be recovered")
        try:
            values = {
                field: data[field]
                for field in ("available_paise", "locked_paise", "total_paise", "version")
            }
            if any(isinstance(value, bool) or not isinstance(value, int) for value in values.values()):
                raise TypeError
            return WalletSnapshot(
                id=UUID(str(data["id"])),
                currency=require_inr_currency(data["currency"]),
                available_paise=values["available_paise"],
                locked_paise=values["locked_paise"],
                total_paise=values["total_paise"],
                version=values["version"],
            )
        except (KeyError, TypeError, ValueError, ValidationError) as error:
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior wallet response cannot be recovered") from error

    @classmethod
    def _hold_snapshot_from_response(
        cls, response_payload: dict[str, object]
    ) -> WalletHoldSnapshot | None:
        data = response_payload.get("wallet_hold")
        if data is None:
            return None
        if not isinstance(data, dict):
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior hold response cannot be recovered")
        try:
            amount = data["amount_paise"]
            if isinstance(amount, bool) or not isinstance(amount, int):
                raise TypeError
            release_group_id = data["release_journal_group_id"]
            settlement_group_id = data.get("settlement_journal_group_id")
            return WalletHoldSnapshot(
                id=UUID(str(data["id"])),
                wallet_id=UUID(str(data["wallet_id"])),
                reference_type=str(data["reference_type"]),
                reference_id=UUID(str(data["reference_id"])),
                amount_paise=require_paise(amount),
                currency=require_inr_currency(data["currency"]),
                status=WalletHoldStatus(str(data["status"])),
                hold_journal_group_id=UUID(str(data["hold_journal_group_id"])),
                release_journal_group_id=(
                    UUID(str(release_group_id)) if release_group_id is not None else None
                ),
                settlement_journal_group_id=(
                    UUID(str(settlement_group_id)) if settlement_group_id is not None else None
                ),
            )
        except (KeyError, TypeError, ValueError, ValidationError) as error:
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior hold response cannot be recovered") from error

    def _response_payload(
        self, *, wallet: Wallet, group: JournalGroup | None, hold: WalletHold | None
    ) -> dict[str, object]:
        return canonical_payload(
            {
                "wallet": self._wallet_state(wallet),
                "journal_group_id": group.id if group is not None else None,
                "wallet_hold_id": hold.id if hold is not None else None,
                "wallet_hold": self._hold_state(hold) if hold is not None else None,
            }
        )
