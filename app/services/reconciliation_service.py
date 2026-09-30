"""Durable, non-settling payment-reconciliation queue snapshots.

Phase 5 has authenticated webhook paths, but not a selected white-label
provider status API or a bank/UPI reconciliation feed. This service records
the exact unresolved work for an operator without treating a queue scan,
manual proof, timeout, or provider-event retry as payment confirmation.

It intentionally does not import the payment orchestrator, ledger, ticket
allocator, or a provider adapter. The only state it changes is the durable
PaymentReconciliationRun record and its audit trail.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditService
from app.exceptions import ConflictError, ValidationError
from app.models.payment import (
    ManualPaymentProof,
    ManualPaymentProofStatus,
    PaymentAttempt,
    PaymentAttemptStatus,
    PaymentMethod,
    PaymentProviderEvent,
    PaymentProviderEventStatus,
    PaymentReconciliationRun,
    PaymentReconciliationStatus,
)
from app.repositories.generic_payment_repository import (
    get_payment_reconciliation_run,
    list_payment_reconciliation_scopes,
)


TELEGRAM_STARS_NAMESPACE = "telegram_stars"
MANUAL_UPI_NAMESPACE = "manual_upi"
RECONCILIATION_FRAMEWORK_VERSION = "phase5-review-queue-v1"


@dataclass(frozen=True, slots=True)
class PaymentReconciliationScope:
    """One bounded payment method/provider namespace to inspect."""

    method: PaymentMethod
    provider_namespace: str


@dataclass(frozen=True, slots=True)
class PaymentReconciliationResult:
    """The durable run returned by a safe reconciliation scan."""

    run: PaymentReconciliationRun
    replayed: bool


class PaymentReconciliationService:
    """Persist review-queue snapshots without fabricating provider settlement."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.audit = AuditService(session)

    async def list_open_scopes(self, *, limit: int) -> list[PaymentReconciliationScope]:
        """List scopes containing attempts/events that require reconciliation.

        The repository returns known attempt scopes and unresolved callback
        namespaces. An unmatched callback has no method field, so the only
        safe mapping is to the platform-owned Telegram/manual namespaces or
        the generic white-label boundary. This mapping controls reporting
        only; it cannot settle anything.
        """

        raw_scopes = await list_payment_reconciliation_scopes(self.session, limit=limit)
        result: list[PaymentReconciliationScope] = []
        seen: set[tuple[PaymentMethod, str]] = set()
        for stored_method, provider_namespace in raw_scopes:
            method = self._coerce_method(stored_method, provider_namespace=provider_namespace)
            key = (method, provider_namespace)
            if key not in seen:
                seen.add(key)
                result.append(PaymentReconciliationScope(*key))
        return result

    async def reconcile_scope(
        self,
        *,
        method: PaymentMethod,
        provider_namespace: str,
        run_key: str,
        window_starts_at: datetime | None = None,
        window_ends_at: datetime | None = None,
        commit: bool,
    ) -> PaymentReconciliationResult:
        """Write one idempotent, bounded snapshot of unresolved payment work.

        A completed run_key replays the previously persisted outcome. A
        stranded pending/running/failed row is safely resumed because this
        method never performs an irreversible provider or settlement action.
        """

        method = self._require_method(method)
        provider_namespace = self._require_text(
            provider_namespace, field="provider_namespace", maximum=64
        )
        run_key = self._require_text(run_key, field="run_key", maximum=255)
        window_starts_at = self._as_utc(window_starts_at, field="window_starts_at")
        window_ends_at = self._as_utc(window_ends_at, field="window_ends_at")
        if (
            window_starts_at is not None
            and window_ends_at is not None
            and window_ends_at < window_starts_at
        ):
            raise ValidationError(
                "INVALID_RECONCILIATION_WINDOW",
                "window_ends_at must not be before window_starts_at",
            )

        run = await get_payment_reconciliation_run(
            self.session, run_key=run_key, for_update=True
        )
        if run is None:
            run = await self._create_or_lock_run(
                method=method,
                provider_namespace=provider_namespace,
                run_key=run_key,
                window_starts_at=window_starts_at,
                window_ends_at=window_ends_at,
            )
        self._assert_scope_matches(
            run=run, method=method, provider_namespace=provider_namespace
        )
        if run.status == PaymentReconciliationStatus.COMPLETED:
            return PaymentReconciliationResult(run=run, replayed=True)

        now = datetime.now(timezone.utc)
        before = self.run_snapshot(run)
        was_resumed = run.status in {
            PaymentReconciliationStatus.RUNNING,
            PaymentReconciliationStatus.FAILED,
        }
        run.status = PaymentReconciliationStatus.RUNNING
        run.started_at = now
        run.completed_at = None
        run.error = None
        run.checkpoint = {
            "framework_version": RECONCILIATION_FRAMEWORK_VERSION,
            "stage": "review_queue_snapshot",
            "safe_mode": "NO_PROVIDER_OR_SETTLEMENT_ACTION",
            # JSON columns must receive portable scalar values. Keep the
            # timestamp in a distinct typed column as well as an ISO string
            # in the operational checkpoint.
            "started_at": now.isoformat(),
        }

        # Do not call a provider here. The summary is intentionally an
        # operational queue snapshot, not a status API response or payment
        # verification result.
        summary = await self._build_summary(
            method=method, provider_namespace=provider_namespace
        )
        run.result_summary = summary
        run.status = PaymentReconciliationStatus.COMPLETED
        run.completed_at = datetime.now(timezone.utc)
        run.checkpoint = {
            "framework_version": RECONCILIATION_FRAMEWORK_VERSION,
            "stage": "review_queue_snapshot_completed",
            "safe_mode": "NO_PROVIDER_OR_SETTLEMENT_ACTION",
            "completed_at": run.completed_at.isoformat(),
        }
        self.audit.record(
            actor_user_id=None,
            entity_type="payment_reconciliation_run",
            entity_id=run.id,
            action=(
                "PAYMENT_RECONCILIATION_RUN_RESUMED"
                if was_resumed
                else "PAYMENT_RECONCILIATION_RUN_COMPLETED"
            ),
            before_state=before,
            after_state=self.run_snapshot(run),
            reason=self._policy_reason(method),
        )
        await self._finish(commit=commit)
        return PaymentReconciliationResult(run=run, replayed=False)

    async def _create_or_lock_run(
        self,
        *,
        method: PaymentMethod,
        provider_namespace: str,
        run_key: str,
        window_starts_at: datetime | None,
        window_ends_at: datetime | None,
    ) -> PaymentReconciliationRun:
        """Create a unique run or lock a concurrent creator's persisted row."""

        created = PaymentReconciliationRun(
            run_key=run_key,
            provider_namespace=provider_namespace,
            method=method,
            status=PaymentReconciliationStatus.PENDING,
            window_starts_at=window_starts_at,
            window_ends_at=window_ends_at,
        )
        try:
            # A savepoint keeps the outer worker transaction usable after a
            # concurrent task wins the unique run-key race.
            async with self.session.begin_nested():
                self.session.add(created)
                await self.session.flush()
        except IntegrityError:
            existing = await get_payment_reconciliation_run(
                self.session, run_key=run_key, for_update=True
            )
            if existing is None:
                raise
            return existing
        return created

    async def _build_summary(
        self, *, method: PaymentMethod, provider_namespace: str
    ) -> dict[str, object]:
        """Count review work only; no result below is payment evidence."""

        attempt_base = (
            PaymentAttempt.method == method,
            PaymentAttempt.provider_namespace == provider_namespace,
        )
        attempts_under_review = await self._count(
            select(func.count())
            .select_from(PaymentAttempt)
            .where(*attempt_base, PaymentAttempt.status == PaymentAttemptStatus.UNDER_REVIEW)
        )
        attempts_expired = await self._count(
            select(func.count())
            .select_from(PaymentAttempt)
            .where(*attempt_base, PaymentAttempt.status == PaymentAttemptStatus.EXPIRED)
        )
        provider_events_review_required = await self._count(
            select(func.count())
            .select_from(PaymentProviderEvent)
            .where(
                PaymentProviderEvent.provider_namespace == provider_namespace,
                PaymentProviderEvent.status == PaymentProviderEventStatus.REVIEW_REQUIRED,
            )
        )
        provider_events_failed = await self._count(
            select(func.count())
            .select_from(PaymentProviderEvent)
            .where(
                PaymentProviderEvent.provider_namespace == provider_namespace,
                PaymentProviderEvent.status == PaymentProviderEventStatus.FAILED,
            )
        )
        unmatched_provider_events = await self._count(
            select(func.count())
            .select_from(PaymentProviderEvent)
            .where(
                PaymentProviderEvent.provider_namespace == provider_namespace,
                PaymentProviderEvent.status.in_(
                    (
                        PaymentProviderEventStatus.REVIEW_REQUIRED,
                        PaymentProviderEventStatus.FAILED,
                    )
                ),
                PaymentProviderEvent.payment_attempt_id.is_(None),
            )
        )
        pending_manual_proofs = await self._count(
            select(func.count())
            .select_from(ManualPaymentProof)
            .join(
                PaymentAttempt,
                ManualPaymentProof.payment_attempt_id == PaymentAttempt.id,
            )
            .where(
                *attempt_base,
                ManualPaymentProof.status.in_(
                    (
                        ManualPaymentProofStatus.SUBMITTED,
                        ManualPaymentProofStatus.UNDER_REVIEW,
                    )
                ),
            )
        )
        action_required = (
            attempts_under_review
            + attempts_expired
            + provider_events_review_required
            + provider_events_failed
            + pending_manual_proofs
        )
        return {
            "framework_version": RECONCILIATION_FRAMEWORK_VERSION,
            "mode": "REVIEW_QUEUE_ONLY",
            "method": method.value,
            "provider_namespace": provider_namespace,
            "remote_provider_status_checked": False,
            "automatic_settlements": 0,
            "attempts_under_review": attempts_under_review,
            "attempts_expired": attempts_expired,
            "provider_events_review_required": provider_events_review_required,
            "provider_events_failed": provider_events_failed,
            "unmatched_provider_events": unmatched_provider_events,
            "pending_manual_proofs": pending_manual_proofs,
            "action_required": action_required,
            "settlement_policy": self._policy_reason(method),
        }

    async def _count(self, statement) -> int:
        return int((await self.session.scalar(statement)) or 0)

    async def _finish(self, *, commit: bool) -> None:
        await self.session.flush()
        if commit:
            await self.session.commit()

    @staticmethod
    def _coerce_method(value: object, *, provider_namespace: str) -> PaymentMethod:
        if isinstance(value, PaymentMethod):
            return value
        if value is not None:
            try:
                return PaymentMethod(str(value))
            except ValueError:
                # A corrupt/legacy enum must not be mistaken for a concrete
                # provider contract. It remains on the generic review path.
                pass
        if provider_namespace == TELEGRAM_STARS_NAMESPACE:
            return PaymentMethod.TELEGRAM_STARS
        if provider_namespace == MANUAL_UPI_NAMESPACE:
            return PaymentMethod.MANUAL_UPI
        return PaymentMethod.WHITE_LABEL

    @staticmethod
    def _require_method(value: object) -> PaymentMethod:
        if not isinstance(value, PaymentMethod):
            raise ValidationError("INVALID_PAYMENT_METHOD", "Unsupported payment method")
        return value

    @staticmethod
    def _require_text(value: object, *, field: str, maximum: int) -> str:
        if not isinstance(value, str):
            raise ValidationError("INVALID_RECONCILIATION_INPUT", f"{field} must be a string")
        normalized = value.strip()
        if not normalized or len(normalized) > maximum:
            raise ValidationError(
                "INVALID_RECONCILIATION_INPUT",
                f"{field} must be a non-empty string up to {maximum} characters",
            )
        return normalized

    @staticmethod
    def _as_utc(value: datetime | None, *, field: str) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValidationError(
                "INVALID_RECONCILIATION_WINDOW", f"{field} must be timezone-aware"
            )
        return value.astimezone(timezone.utc)

    @staticmethod
    def _assert_scope_matches(
        *, run: PaymentReconciliationRun, method: PaymentMethod, provider_namespace: str
    ) -> None:
        if run.method != method or run.provider_namespace != provider_namespace:
            raise ConflictError(
                "RECONCILIATION_RUN_KEY_CONFLICT",
                "run_key was already used for a different payment reconciliation scope",
            )

    @staticmethod
    def _policy_reason(method: PaymentMethod) -> str:
        if method == PaymentMethod.MANUAL_UPI:
            return (
                "Manual UPI evidence requires an accountable administrator decision; "
                "this queue scan cannot verify a bank transfer or settle an order."
            )
        if method == PaymentMethod.TELEGRAM_STARS:
            return (
                "Telegram Stars reconciliation has no configured transaction-polling adapter; "
                "only an authenticated successful_payment update may settle an order."
            )
        return (
            "No concrete white-label provider reconciliation adapter is configured; "
            "this queue scan cannot verify or settle a provider payment."
        )

    @staticmethod
    def run_snapshot(run: PaymentReconciliationRun) -> dict[str, object]:
        return {
            "id": run.id,
            "run_key": run.run_key,
            "provider_namespace": run.provider_namespace,
            "method": run.method,
            "status": run.status,
            "window_starts_at": run.window_starts_at,
            "window_ends_at": run.window_ends_at,
            "checkpoint": run.checkpoint,
            "result_summary": run.result_summary,
            "started_at": run.started_at,
            "completed_at": run.completed_at,
            "error": run.error,
        }
