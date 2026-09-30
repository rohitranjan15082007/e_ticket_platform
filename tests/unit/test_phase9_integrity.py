"""Read-only Phase 9 journal and wallet-cache reconciliation tests."""

import json
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ledger import (
    PLATFORM_CLEARING_ACCOUNT_ID,
    AccountKind, JournalGroup, JournalPosting, LedgerAccount, PostingDirection,
)
from app.models.user import IdempotencyRecord, User
from app.models.wallet import Wallet
from app.services.integrity_service import IntegrityReport, IntegrityService
from scripts import reconcile_ledger
from scripts.reconcile_ledger import report_payload


async def seed_wallet_and_group(session: AsyncSession) -> tuple[Wallet, LedgerAccount]:
    user = User(email=f"integrity-{uuid4()}@example.test", password_hash="test-hash")
    session.add(user)
    await session.flush()
    wallet = Wallet(user_id=user.id, currency="INR", available_paise=100, locked_paise=0)
    session.add(wallet)
    await session.flush()
    available = LedgerAccount(
        code=f"wallet:{wallet.id}:available", kind=AccountKind.USER_AVAILABLE,
        currency="INR", wallet_id=wallet.id, user_id=user.id,
    )
    held = LedgerAccount(
        code=f"wallet:{wallet.id}:held", kind=AccountKind.USER_WITHDRAWAL_HELD,
        currency="INR", wallet_id=wallet.id, user_id=user.id,
    )
    session.add_all((available, held))
    await session.flush()
    await add_group(session, available=available, debit=100, credit=100)
    await session.commit()
    return wallet, available


async def add_group(
    session: AsyncSession, *, available: LedgerAccount, debit: int, credit: int,
) -> JournalGroup:
    record = IdempotencyRecord(
        actor_scope="system:integrity-test", idempotency_key=str(uuid4()),
        request_fingerprint="a" * 64, resource_type="journal_group", status_code=201,
    )
    session.add(record)
    await session.flush()
    group = JournalGroup(
        event_type="INTEGRITY_TEST", currency="INR", reference_type="test",
        idempotency_record_id=record.id,
    )
    session.add(group)
    await session.flush()
    session.add_all((
        JournalPosting(
            journal_group_id=group.id, account_id=PLATFORM_CLEARING_ACCOUNT_ID,
            direction=PostingDirection.DEBIT, amount_paise=debit, currency="INR",
        ),
        JournalPosting(
            journal_group_id=group.id, account_id=available.id,
            direction=PostingDirection.CREDIT, amount_paise=credit, currency="INR",
        ),
    ))
    return group


@pytest.mark.asyncio
async def test_integrity_scan_accepts_balanced_journal_and_matching_wallet(session: AsyncSession) -> None:
    await seed_wallet_and_group(session)
    report = await IntegrityService(session).scan()
    assert report.healthy
    assert report.journal_groups_checked == 1
    assert report.wallets_checked == 1
    assert report_payload(report)["status"] == "ok"


@pytest.mark.asyncio
async def test_integrity_scan_reconstructs_available_and_held_balances(session: AsyncSession) -> None:
    wallet, available = await seed_wallet_and_group(session)
    held = await session.scalar(
        select(LedgerAccount).where(
            LedgerAccount.wallet_id == wallet.id,
            LedgerAccount.kind == AccountKind.USER_WITHDRAWAL_HELD,
        )
    )
    assert held is not None
    record = IdempotencyRecord(
        actor_scope="system:integrity-test", idempotency_key=str(uuid4()),
        request_fingerprint="b" * 64, resource_type="journal_group", status_code=201,
    )
    session.add(record)
    await session.flush()
    group = JournalGroup(
        event_type="WALLET_HELD", currency="INR", reference_type="test",
        idempotency_record_id=record.id,
    )
    session.add(group)
    await session.flush()
    session.add_all((
        JournalPosting(
            journal_group_id=group.id, account_id=available.id,
            direction=PostingDirection.DEBIT, amount_paise=30, currency="INR",
        ),
        JournalPosting(
            journal_group_id=group.id, account_id=held.id,
            direction=PostingDirection.CREDIT, amount_paise=30, currency="INR",
        ),
    ))
    wallet.available_paise = 70
    wallet.locked_paise = 30
    await session.commit()

    report = await IntegrityService(session).scan()
    assert report.healthy
    assert report.journal_groups_checked == 2
    assert report.wallets_checked == 1


@pytest.mark.asyncio
async def test_integrity_scan_reports_both_imbalances_without_mutation(session: AsyncSession) -> None:
    wallet, available = await seed_wallet_and_group(session)
    group = await add_group(session, available=available, debit=25, credit=24)
    wallet.available_paise = 99
    await session.commit()

    report = await IntegrityService(session).scan()
    assert not report.healthy
    assert report.journal_groups_checked == 2
    assert report.unbalanced_group_ids == (group.id,)
    assert report.wallet_mismatch_ids == (wallet.id,)
    assert report_payload(report)["status"] == "finding"
    assert wallet.available_paise == 99

    journal_only = await IntegrityService(session).scan(include_wallets=False)
    assert journal_only.wallets_checked == 0
    assert journal_only.unbalanced_group_count == 1


@pytest.mark.asyncio
async def test_integrity_scan_rejects_unbounded_finding_output(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="max_findings"):
        await IntegrityService(session).scan(max_findings=0)


@pytest.mark.asyncio
async def test_cli_uses_nonzero_exit_for_findings_without_changing_state(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    class Context:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    class Scanner:
        async def scan(self, **_kwargs):
            return IntegrityReport(
                journal_groups_checked=2, unbalanced_group_count=1,
                unbalanced_group_ids=(uuid4(),), wallets_checked=1,
                wallet_mismatch_count=0, wallet_mismatch_ids=(), findings_truncated=False,
            )

    monkeypatch.setattr(reconcile_ledger, "SessionLocal", Context)
    monkeypatch.setattr(reconcile_ledger, "IntegrityService", lambda _session: Scanner())
    assert await reconcile_ledger.run_check() == 2
    assert json.loads(capsys.readouterr().out)["status"] == "finding"


@pytest.mark.asyncio
async def test_cli_hides_connection_error_details(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    class BrokenContext:
        async def __aenter__(self):
            raise RuntimeError("secret database url")

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(reconcile_ledger, "SessionLocal", BrokenContext)
    assert await reconcile_ledger.run_check() == 3
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err)["error_type"] == "RuntimeError"
    assert "secret database url" not in output.err
