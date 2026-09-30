"""Wallet mutations must be balanced, traceable, authorized, and replay-safe."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.exceptions import AuthorizationError, ConflictError, ValidationError
from app.models.audit_log import AuditLog
from app.models.ledger import JournalGroup
from app.models.user import IdempotencyRecord, User
from app.models.wallet import Wallet, WalletHoldStatus
from app.services.ledger_service import LedgerService
from app.services.wallet_service import WalletMutationResult, WalletService, WalletSnapshot


async def create_user(session: AsyncSession, email: str = "wallet-user@example.com") -> User:
    user = User(email=email, password_hash=hash_password("correct-horse-battery-staple"))
    session.add(user)
    await session.commit()
    return user


async def provision_wallet(
    session: AsyncSession, user: User, *, idempotency_key: str
) -> WalletSnapshot:
    result = await WalletService(session).provision_wallet(
        user_id=user.id,
        actor_user_id=user.id,
        currency="INR",
        idempotency_key=idempotency_key,
        commit=True,
    )
    return result.wallet


async def credit_wallet(
    session: AsyncSession,
    wallet_id: UUID,
    *,
    amount_paise: int,
    idempotency_key: str,
    source_id: UUID,
) -> WalletMutationResult:
    return await WalletService(session).credit_available(
        wallet_id=wallet_id,
        actor_user_id=None,
        amount_paise=amount_paise,
        idempotency_key=idempotency_key,
        source_type="HOST_CREDIT",
        source_id=source_id,
        reason="host accounting credit",
        commit=True,
    )


@pytest.mark.asyncio
async def test_wallet_credit_hold_and_release_are_traceable_balanced_and_audited(
    session: AsyncSession,
) -> None:
    user = await create_user(session)
    service = WalletService(session)
    wallet = await provision_wallet(session, user, idempotency_key="wallet-provision-001")
    source_id = uuid4()
    credited = await credit_wallet(
        session,
        wallet.id,
        amount_paise=10_000,
        idempotency_key="wallet-credit-001",
        source_id=source_id,
    )
    reference_id = uuid4()
    held = await service.hold_available(
        wallet_id=wallet.id,
        actor_user_id=user.id,
        amount_paise=4_000,
        idempotency_key="wallet-hold-001",
        reference_type="withdrawal_request",
        reference_id=reference_id,
        reason="reserve a named withdrawal hold",
        commit=True,
    )
    released = await service.release_locked(
        wallet_id=wallet.id,
        actor_user_id=user.id,
        amount_paise=4_000,
        idempotency_key="wallet-release-001",
        reference_type="withdrawal_request",
        reference_id=reference_id,
        reason="cancel the same named hold",
        commit=True,
    )

    assert credited.journal_group is not None
    assert credited.journal_group.reference_type == "HOST_CREDIT"
    assert credited.journal_group.reference_id == source_id
    assert credited.journal_group.idempotency_record_id is not None
    credit_record = await session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.actor_scope == "system:wallet.credit",
            IdempotencyRecord.idempotency_key == "wallet-credit-001",
        )
    )
    assert credit_record is not None
    assert credit_record.resource_type == "journal_group"
    assert credit_record.resource_id == credited.journal_group.id
    assert credit_record.id == credited.journal_group.idempotency_record_id

    assert held.wallet_hold is not None
    assert released.wallet_hold is not None
    assert released.wallet_hold.id == held.wallet_hold.id
    assert released.wallet_hold.status == WalletHoldStatus.RELEASED
    assert released.wallet_hold.hold_journal_group_id == held.journal_group.id
    assert released.wallet_hold.release_journal_group_id == released.journal_group.id
    assert released.wallet.available_paise == 10_000
    assert released.wallet.locked_paise == 0
    assert released.wallet.available_paise + released.wallet.locked_paise == 10_000
    for result in (credited, held, released):
        assert result.journal_group is not None
        await LedgerService(session).assert_group_balanced(result.journal_group.id)
    assert await session.scalar(select(func.count()).select_from(AuditLog)) == 6
    assert await session.scalar(select(func.count()).select_from(JournalGroup)) == 3


@pytest.mark.asyncio
async def test_credit_replay_returns_the_original_response_snapshot_after_wallet_changes(
    session: AsyncSession,
) -> None:
    user = await create_user(session)
    service = WalletService(session)
    wallet = await provision_wallet(session, user, idempotency_key="wallet-provision-002")
    source_id = uuid4()
    first = await credit_wallet(
        session,
        wallet.id,
        amount_paise=1_000,
        idempotency_key="wallet-credit-002",
        source_id=source_id,
    )
    first_snapshot = deepcopy(first.response_payload)
    await service.hold_available(
        wallet_id=wallet.id,
        actor_user_id=user.id,
        amount_paise=400,
        idempotency_key="wallet-hold-002",
        reference_type="withdrawal_request",
        reference_id=uuid4(),
        reason="change the live wallet after the credit",
        commit=True,
    )
    replay = await credit_wallet(
        session,
        wallet.id,
        amount_paise=1_000,
        idempotency_key="wallet-credit-002",
        source_id=source_id,
    )

    assert not first.replayed
    assert replay.replayed
    assert replay.journal_group is not None
    assert replay.journal_group.id == first.journal_group.id
    assert replay.response_payload == first_snapshot
    assert replay.response_payload["wallet"] == {
        "id": str(wallet.id),
        "currency": "INR",
        "available_paise": 1_000,
        "locked_paise": 0,
        "total_paise": 1_000,
        "version": 1,
    }
    assert replay.wallet.available_paise == 1_000
    assert replay.wallet.locked_paise == 0
    assert await session.scalar(select(func.count()).select_from(JournalGroup)) == 2


@pytest.mark.asyncio
async def test_named_hold_replays_once_and_requires_an_exact_named_release(
    session: AsyncSession,
) -> None:
    user = await create_user(session)
    service = WalletService(session)
    wallet = await provision_wallet(session, user, idempotency_key="wallet-provision-003")
    await credit_wallet(
        session,
        wallet.id,
        amount_paise=500,
        idempotency_key="wallet-credit-003",
        source_id=uuid4(),
    )
    reference_id = uuid4()
    first_hold = await service.hold_available(
        wallet_id=wallet.id,
        actor_user_id=user.id,
        amount_paise=300,
        idempotency_key="wallet-hold-003",
        reference_type="withdrawal_request",
        reference_id=reference_id,
        reason="one named hold",
        commit=True,
    )
    replayed_hold = await service.hold_available(
        wallet_id=wallet.id,
        actor_user_id=user.id,
        amount_paise=300,
        idempotency_key="wallet-hold-003",
        reference_type="withdrawal_request",
        reference_id=reference_id,
        reason="one named hold",
        commit=True,
    )

    assert first_hold.wallet_hold is not None
    assert replayed_hold.replayed
    assert replayed_hold.wallet_hold is not None
    assert replayed_hold.wallet_hold.id == first_hold.wallet_hold.id
    assert await session.scalar(select(func.count()).select_from(JournalGroup)) == 2

    with pytest.raises(ConflictError, match="WALLET_HOLD_ALREADY_EXISTS"):
        await service.hold_available(
            wallet_id=wallet.id,
            actor_user_id=user.id,
            amount_paise=300,
            idempotency_key="wallet-hold-003-different-key",
            reference_type="withdrawal_request",
            reference_id=reference_id,
            reason="a duplicate named hold",
            commit=True,
        )
    with pytest.raises(ConflictError, match="WALLET_HOLD_AMOUNT_MISMATCH"):
        await service.release_locked(
            wallet_id=wallet.id,
            actor_user_id=user.id,
            amount_paise=299,
            idempotency_key="wallet-release-003-wrong-amount",
            reference_type="withdrawal_request",
            reference_id=reference_id,
            reason="partial releases are not allowed",
            commit=True,
        )

    released = await service.release_locked(
        wallet_id=wallet.id,
        actor_user_id=user.id,
        amount_paise=300,
        idempotency_key="wallet-release-003",
        reference_type="withdrawal_request",
        reference_id=reference_id,
        reason="release the full named hold",
        commit=True,
    )
    replayed_release = await service.release_locked(
        wallet_id=wallet.id,
        actor_user_id=user.id,
        amount_paise=300,
        idempotency_key="wallet-release-003",
        reference_type="withdrawal_request",
        reference_id=reference_id,
        reason="release the full named hold",
        commit=True,
    )

    assert released.wallet_hold is not None
    assert released.wallet_hold.status == WalletHoldStatus.RELEASED
    assert replayed_release.replayed
    assert replayed_release.wallet_hold is not None
    assert replayed_release.wallet_hold.id == released.wallet_hold.id
    assert released.wallet.available_paise == 500
    assert released.wallet.locked_paise == 0
    assert await session.scalar(select(func.count()).select_from(JournalGroup)) == 3


@pytest.mark.asyncio
async def test_commit_false_flushes_but_a_caller_rollback_removes_every_credit_side_effect(
    session: AsyncSession,
) -> None:
    user = await create_user(session)
    service = WalletService(session)
    wallet = await provision_wallet(session, user, idempotency_key="wallet-provision-004")
    wallet_id = wallet.id

    result = await service.credit_available(
        wallet_id=wallet_id,
        actor_user_id=None,
        amount_paise=900,
        idempotency_key="wallet-credit-004",
        source_type="HOST_CREDIT",
        source_id=uuid4(),
        reason="rollback test source",
        commit=False,
    )
    assert result.wallet.available_paise == 900
    assert result.journal_group is not None
    await session.rollback()

    reloaded_wallet = await session.scalar(select(Wallet).where(Wallet.id == wallet_id))
    assert reloaded_wallet is not None
    assert reloaded_wallet.available_paise == 0
    assert reloaded_wallet.locked_paise == 0
    assert await session.scalar(select(func.count()).select_from(JournalGroup)) == 0
    assert await session.scalar(select(func.count()).select_from(AuditLog)) == 1
    assert await session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 1


@pytest.mark.asyncio
async def test_wallet_mutations_require_authorization_inr_and_traceable_uuid_references(
    session: AsyncSession,
) -> None:
    owner = await create_user(session, "wallet-owner@example.com")
    outsider = await create_user(session, "wallet-outsider@example.com")
    service = WalletService(session)

    with pytest.raises(ValidationError, match="UNSUPPORTED_CURRENCY"):
        await service.provision_wallet(
            user_id=owner.id,
            actor_user_id=owner.id,
            currency="USD",
            idempotency_key="wallet-provision-usd",
            commit=True,
        )
    wallet = await provision_wallet(session, owner, idempotency_key="wallet-provision-005")

    with pytest.raises(AuthorizationError):
        await service.credit_available(
            wallet_id=wallet.id,
            actor_user_id=owner.id,
            amount_paise=100,
            idempotency_key="wallet-credit-owner-forbidden",
            source_type="HOST_CREDIT",
            source_id=uuid4(),
            reason="an owner cannot invent wallet credits",
            commit=True,
        )
    with pytest.raises(AuthorizationError):
        await service.hold_available(
            wallet_id=wallet.id,
            actor_user_id=outsider.id,
            amount_paise=1,
            idempotency_key="wallet-hold-outsider-forbidden",
            reference_type="withdrawal_request",
            reference_id=uuid4(),
            reason="an outsider cannot hold another wallet",
            commit=True,
        )
    with pytest.raises(ValidationError, match="INVALID_REFERENCE"):
        await service.credit_available(
            wallet_id=wallet.id,
            actor_user_id=None,
            amount_paise=100,
            idempotency_key="wallet-credit-missing-source",
            source_type="HOST_CREDIT",
            source_id=None,  # type: ignore[arg-type]
            reason="a credit needs a stable source UUID",
            commit=True,
        )
    with pytest.raises(ValidationError, match="INVALID_REFERENCE"):
        await service.hold_available(
            wallet_id=wallet.id,
            actor_user_id=owner.id,
            amount_paise=1,
            idempotency_key="wallet-hold-missing-reference",
            reference_type="withdrawal_request",
            reference_id=None,  # type: ignore[arg-type]
            reason="a hold needs a stable business reference UUID",
            commit=True,
        )

    assert await session.scalar(select(func.count()).select_from(JournalGroup)) == 0
    reloaded_wallet = await session.scalar(select(Wallet).where(Wallet.id == wallet.id))
    assert reloaded_wallet is not None
    assert reloaded_wallet.available_paise == 0
    assert reloaded_wallet.locked_paise == 0
