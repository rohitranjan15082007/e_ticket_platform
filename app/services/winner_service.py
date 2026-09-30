"""Commit/reveal winner selection, prize posting, and result publication.

The service never accepts a winning ticket identifier from an administrator.
It freezes allocated tickets at sales closure and uses deterministic SHA-256
rejection sampling after a pre-sale seed commitment is revealed.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import hmac
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.audit import AuditService
from app.core.idempotency import IdempotencyService, canonical_payload, fingerprint
from app.core.permissions import RoleName
from app.core.state_machine import SERIES_TRANSITIONS, require_transition
from app.core.transaction_locks import lock_key_for_transaction
from app.exceptions import AuthorizationError, ConflictError, ValidationError
from app.models.ticket import Ticket, TicketStatus
from app.models.ticket_series import TicketSeries, TicketSeriesPrize, TicketSeriesStatus
from app.models.user import IdempotencyRecord, User
from app.models.winner import Draw, DrawEntry, DrawNotification, DrawStatus, PrizeAward, Winner
from app.repositories.ticket_repository import get_ticket_series
from app.repositories.wallet_repository import get_wallet_by_user_id
from app.repositories.winner_repository import (
    count_draw_entries,
    get_draw,
    get_draw_by_series,
    list_draw_entry_serial_numbers,
    list_draw_entries_for_tickets,
    list_draw_entries,
    list_draw_notifications,
    list_prize_awards,
    list_winners,
)
from app.services.ticket_series_service import TicketSeriesService
from app.services.wallet_service import WalletService


DRAW_ALGORITHM_VERSION = "sha256-rejection-sampling-v1"
RESULT_NOTIFICATION_EVENT = "DRAW_RESULT_PUBLISHED"


@dataclass(slots=True)
class DrawMutationResult:
    """One durable draw mutation and its replayable response snapshot."""

    draw: Draw
    series: TicketSeries
    response_payload: dict[str, object]
    replayed: bool


class WinnerService:
    """Own the complete, ordered Phase 6 draw lifecycle.

    A production deployment should publish the commitment to an independent
    verifier before closure. This implementation makes the transcript and
    algorithm reproducible locally; it does not claim an external randomness
    beacon or a third-party fairness certification.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.audit = AuditService(session)
        self.idempotency = IdempotencyService(session)

    async def commit_seed(
        self,
        *,
        series_id: UUID,
        actor_user_id: UUID,
        seed_commitment: str,
        idempotency_key: str,
        commit: bool,
    ) -> DrawMutationResult:
        """Store a SHA-256 commitment before the published series can open for sales."""

        await self._require_admin(actor_user_id)
        commitment = self._validate_commitment(seed_commitment)
        scope = f"admin:{actor_user_id}:draw.commit"
        request = {"action": "commit", "series_id": series_id, "seed_commitment": commitment}
        request_fingerprint = fingerprint(request)
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay(replay)

        # A missing draw has no row to lock. The advisory lock prevents two
        # different idempotency keys from both creating a commitment.
        await lock_key_for_transaction(self.session, "draw-series", str(series_id))
        series = await self._locked_series(series_id)
        existing = await get_draw_by_series(self.session, series.id, for_update=True)
        if existing is not None:
            raise ConflictError("DRAW_ALREADY_COMMITTED", "This ticket series already has a draw commitment")
        now = self._now()
        if series.status != TicketSeriesStatus.PUBLISHED:
            raise ConflictError(
                "DRAW_COMMIT_SERIES_NOT_PUBLISHED",
                "A draw must be committed while the series is published and before sales open",
            )
        if now >= self._utc(series.sales_end_at):
            raise ConflictError("DRAW_COMMIT_TOO_LATE", "A draw commitment must be recorded before sales close")
        if series.sold_count or series.reserved_count:
            raise ConflictError(
                "DRAW_COMMITMENT_TOO_LATE",
                "A draw commitment must be recorded before any sale or reservation",
            )
        allocated_ticket = await self.session.scalar(
            select(Ticket.id)
            .where(Ticket.series_id == series.id, Ticket.status == TicketStatus.ALLOCATED)
            .limit(1)
            .with_for_update()
        )
        if allocated_ticket is not None:
            raise ConflictError(
                "DRAW_COMMITMENT_TOO_LATE",
                "A draw commitment must be recorded before any allocated ticket",
            )

        draw = Draw(
            id=uuid4(),
            series_id=series.id,
            status=DrawStatus.COMMITTED,
            algorithm_version=DRAW_ALGORITHM_VERSION,
            seed_commitment=commitment,
            eligible_ticket_count=0,
            committed_by_user_id=actor_user_id,
            committed_at=now,
        )
        self.session.add(draw)
        record = self.idempotency.record(
            actor_scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint,
            resource_type="draw",
            resource_id=draw.id,
            status_code=201,
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="draw",
            entity_id=draw.id,
            action="DRAW_SEED_COMMITTED",
            before_state=None,
            after_state=self._draw_state(draw),
        )
        await self.session.flush()
        response = await self._admin_response(series, draw)
        record.response_payload = response
        await self._finish(commit=commit)
        return DrawMutationResult(draw, series, response, False)

    async def close_sales(
        self,
        *,
        series_id: UUID,
        actor_user_id: UUID | None,
        idempotency_key: str,
        commit: bool,
    ) -> DrawMutationResult:
        """Freeze allocated tickets and prize ranks after the sales window ends.

        A scheduled worker may pass ``actor_user_id=None``. It cannot create a
        commitment, reveal a seed, select winners, credit prizes, or publish a
        result; it only performs this safe time-based closure.
        """

        if actor_user_id is not None:
            await self._require_admin(actor_user_id)
        scope = "system:draw.close" if actor_user_id is None else f"admin:{actor_user_id}:draw.close"
        request = {"action": "close_sales", "series_id": series_id}
        request_fingerprint = fingerprint(request)
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay(replay)

        await lock_key_for_transaction(self.session, "draw-series", str(series_id))
        series = await self._locked_series(series_id)
        draw = await get_draw_by_series(self.session, series.id, for_update=True)
        if draw is None:
            raise ConflictError("DRAW_COMMITMENT_REQUIRED", "Sales cannot close without a pre-sale draw commitment")
        now = self._now()
        if series.status != TicketSeriesStatus.OPEN:
            raise ConflictError("DRAW_SALES_ALREADY_CLOSED", "This ticket series is not open for draw closure")
        if draw.status != DrawStatus.COMMITTED:
            raise ConflictError("DRAW_CLOSE_INVALID_STATE", "The draw is not waiting for sales closure")
        if now < self._utc(series.sales_end_at):
            raise ConflictError("DRAW_SALES_WINDOW_ACTIVE", "Sales cannot close before the configured end time")
        if series.reserved_count:
            raise ConflictError(
                "DRAW_ACTIVE_RESERVATIONS",
                "Sales closure waits for all active ticket reservations to resolve safely",
            )

        prizes = self._prize_snapshot_from_series(series.prizes)
        tickets = list(
            await self.session.scalars(
                select(Ticket)
                .where(Ticket.series_id == series.id, Ticket.status == TicketStatus.ALLOCATED)
                .order_by(Ticket.serial_number, Ticket.id)
                .with_for_update()
            )
        )
        if len(tickets) < len(prizes):
            raise ConflictError(
                "DRAW_INSUFFICIENT_ELIGIBLE_TICKETS",
                "The series has fewer allocated tickets than configured prize ranks",
            )
        if any(ticket.is_winner or ticket.prize_paise for ticket in tickets):
            raise ConflictError("DRAW_TICKET_STATE_INVALID", "An eligible ticket already has winner state")

        entry_values = [self._ticket_candidate_state(ticket) for ticket in tickets]
        candidate_digest = self._candidate_digest(
            series_id=series.id, algorithm_version=draw.algorithm_version, entries=entry_values
        )
        before_draw = self._draw_state(draw)
        before_series = TicketSeriesService.snapshot(series)
        for ticket in tickets:
            self.session.add(
                DrawEntry(
                    id=uuid4(),
                    draw_id=draw.id,
                    ticket_id=ticket.id,
                    serial_number=ticket.serial_number,
                    owner_user_id=ticket.owner_user_id,
                )
            )
        draw.status = DrawStatus.SALES_CLOSED
        draw.eligible_ticket_count = len(tickets)
        draw.eligible_tickets_digest = candidate_digest
        draw.prize_snapshot = prizes
        draw.closed_by_user_id = actor_user_id
        draw.closed_at = now
        require_transition(
            current=series.status,
            target=TicketSeriesStatus.CLOSED,
            transitions=SERIES_TRANSITIONS,
            resource="Ticket series",
        )
        series.status = TicketSeriesStatus.CLOSED
        record = self.idempotency.record(
            actor_scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint,
            resource_type="draw",
            resource_id=draw.id,
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="draw",
            entity_id=draw.id,
            action="DRAW_SALES_CLOSED_AND_CANDIDATES_FROZEN",
            before_state=before_draw,
            after_state=self._draw_state(draw),
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="ticket_series",
            entity_id=series.id,
            action="TICKET_SERIES_CLOSED_FOR_DRAW",
            before_state=before_series,
            after_state=TicketSeriesService.snapshot(series),
        )
        await self.session.flush()
        response = await self._admin_response(series, draw)
        record.response_payload = response
        await self._finish(commit=commit)
        return DrawMutationResult(draw, series, response, False)

    async def run_draw(
        self,
        *,
        series_id: UUID,
        actor_user_id: UUID,
        seed_reveal: str,
        idempotency_key: str,
        commit: bool,
    ) -> DrawMutationResult:
        """Verify the seed commitment and deterministically select frozen entries."""

        await self._require_admin(actor_user_id)
        reveal = self._validate_reveal(seed_reveal)
        scope = f"admin:{actor_user_id}:draw.run"
        request = {"action": "run", "series_id": series_id, "seed_reveal": reveal}
        request_fingerprint = fingerprint(request)
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay(replay)

        series = await self._locked_series(series_id)
        draw = await self._locked_draw_for_series(series.id)
        self._require_supported_algorithm(draw)
        now = self._now()
        if series.status != TicketSeriesStatus.CLOSED or draw.status != DrawStatus.SALES_CLOSED:
            raise ConflictError("DRAW_NOT_READY", "The draw requires a safely closed ticket series")
        if now < self._utc(series.draw_at):
            raise ConflictError("DRAW_TIME_NOT_REACHED", "The configured draw time has not been reached")
        if not hmac.compare_digest(sha256(reveal.encode("utf-8")).hexdigest(), draw.seed_commitment):
            raise ValidationError("DRAW_SEED_COMMITMENT_MISMATCH", "seed_reveal does not match the committed digest")

        entries = await list_draw_entries(self.session, draw.id, for_update=True)
        prizes = self._prizes_from_draw(draw)
        if len(entries) != draw.eligible_ticket_count or len(entries) < len(prizes):
            raise ConflictError("DRAW_SNAPSHOT_INVALID", "The frozen draw snapshot is incomplete")
        recomputed_digest = self._candidate_digest(
            series_id=series.id,
            algorithm_version=draw.algorithm_version,
            entries=[self._entry_candidate_state(entry) for entry in entries],
        )
        if not draw.eligible_tickets_digest or not hmac.compare_digest(recomputed_digest, draw.eligible_tickets_digest):
            raise ConflictError("DRAW_SNAPSHOT_DIGEST_MISMATCH", "The frozen draw snapshot did not verify")

        ticket_ids = [entry.ticket_id for entry in entries]
        tickets = list(
            await self.session.scalars(select(Ticket).where(Ticket.id.in_(ticket_ids)).with_for_update())
        )
        ticket_by_id = {ticket.id: ticket for ticket in tickets}
        if len(ticket_by_id) != len(entries):
            raise ConflictError("DRAW_SNAPSHOT_INVALID", "A frozen candidate ticket is unavailable")
        for entry in entries:
            ticket = ticket_by_id[entry.ticket_id]
            if (
                ticket.series_id != series.id
                or ticket.status != TicketStatus.ALLOCATED
                or ticket.owner_user_id != entry.owner_user_id
                or ticket.serial_number != entry.serial_number
                or ticket.is_winner
                or ticket.prize_paise
            ):
                raise ConflictError("DRAW_TICKET_STATE_INVALID", "A frozen candidate ticket is no longer eligible")

        available_entries = list(entries)
        selected: list[tuple[dict[str, object], DrawEntry, int, str]] = []
        for prize in prizes:
            selected_index, counter = self._select_index(
                seed_reveal=reveal,
                eligible_tickets_digest=draw.eligible_tickets_digest,
                rank=int(prize["rank"]),
                population_size=len(available_entries),
            )
            entry = available_entries.pop(selected_index)
            selection_digest = fingerprint(
                {
                    "algorithm_version": draw.algorithm_version,
                    "eligible_tickets_digest": draw.eligible_tickets_digest,
                    "rank": prize["rank"],
                    "selection_counter": counter,
                    "ticket_serial_number": entry.serial_number,
                }
            )
            selected.append((prize, entry, counter, selection_digest))

        before_draw = self._draw_state(draw)
        before_series = TicketSeriesService.snapshot(series)
        winner_transcript: list[dict[str, object]] = []
        for prize, entry, counter, selection_digest in selected:
            ticket = ticket_by_id[entry.ticket_id]
            before_ticket = self._ticket_state(ticket)
            winner = Winner(
                id=uuid4(),
                draw_id=draw.id,
                ticket_id=ticket.id,
                owner_user_id=ticket.owner_user_id,
                rank=int(prize["rank"]),
                title=str(prize["title"]),
                prize_paise=int(prize["prize_paise"]),
                selection_counter=counter,
                selection_digest=selection_digest,
            )
            self.session.add(winner)
            ticket.is_winner = True
            ticket.prize_paise = winner.prize_paise
            winner_state = self._winner_state(winner, ticket_serial_number=ticket.serial_number)
            winner_transcript.append(self._public_winner_state(winner, ticket.serial_number))
            self.audit.record(
                actor_user_id=actor_user_id,
                entity_type="ticket",
                entity_id=ticket.id,
                action="TICKET_MARKED_DRAW_WINNER",
                before_state=before_ticket,
                after_state=self._ticket_state(ticket),
            )
            self.audit.record(
                actor_user_id=actor_user_id,
                entity_type="winner",
                entity_id=winner.id,
                action="DRAW_WINNER_SELECTED",
                before_state=None,
                after_state=winner_state,
            )

        draw.seed_reveal = reveal
        draw.result_digest = self._result_digest(draw=draw, prizes=prizes, winners=winner_transcript)
        draw.status = DrawStatus.DRAWN
        draw.drawn_by_user_id = actor_user_id
        draw.drawn_at = now
        require_transition(
            current=series.status,
            target=TicketSeriesStatus.DRAWN,
            transitions=SERIES_TRANSITIONS,
            resource="Ticket series",
        )
        series.status = TicketSeriesStatus.DRAWN
        record = self.idempotency.record(
            actor_scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint,
            resource_type="draw",
            resource_id=draw.id,
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="draw",
            entity_id=draw.id,
            action="DRAW_WINNERS_SELECTED",
            before_state=before_draw,
            after_state=self._draw_state(draw),
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="ticket_series",
            entity_id=series.id,
            action="TICKET_SERIES_DRAWN",
            before_state=before_series,
            after_state=TicketSeriesService.snapshot(series),
        )
        await self.session.flush()
        response = await self._admin_response(series, draw)
        record.response_payload = response
        await self._finish(commit=commit)
        return DrawMutationResult(draw, series, response, False)

    async def post_prizes(
        self,
        *,
        series_id: UUID,
        actor_user_id: UUID,
        idempotency_key: str,
        commit: bool,
    ) -> DrawMutationResult:
        """Credit every selected prize once in one ledger-backed transaction."""

        await self._require_admin(actor_user_id)
        scope = f"admin:{actor_user_id}:draw.post-prizes"
        request = {"action": "post_prizes", "series_id": series_id}
        request_fingerprint = fingerprint(request)
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay(replay)

        series = await self._locked_series(series_id)
        draw = await self._locked_draw_for_series(series.id)
        if series.status != TicketSeriesStatus.DRAWN or draw.status != DrawStatus.DRAWN:
            raise ConflictError("DRAW_PRIZES_NOT_READY", "Prizes can be posted only after winner selection")
        winners = await list_winners(self.session, draw.id, for_update=True)
        prizes = self._prizes_from_draw(draw)
        if len(winners) != len(prizes):
            raise ConflictError("DRAW_WINNERS_INCOMPLETE", "The draw winner record is incomplete")
        if await list_prize_awards(self.session, draw.id, for_update=True):
            raise ConflictError("DRAW_PRIZE_AWARD_STATE_INVALID", "Prize awards already exist before completion")

        now = self._now()
        wallet_service = WalletService(self.session)
        for winner in sorted(winners, key=lambda value: (str(value.owner_user_id), str(value.id))):
            wallet = await get_wallet_by_user_id(self.session, winner.owner_user_id, for_update=True)
            if wallet is None:
                provisioned = await wallet_service.provision_wallet(
                    user_id=winner.owner_user_id,
                    actor_user_id=actor_user_id,
                    currency="INR",
                    idempotency_key=f"draw-wallet:{winner.owner_user_id}",
                    commit=False,
                )
                wallet_id = provisioned.wallet.id
            else:
                wallet_id = wallet.id
            credited = await wallet_service.credit_available(
                wallet_id=wallet_id,
                actor_user_id=actor_user_id,
                amount_paise=winner.prize_paise,
                idempotency_key=f"draw-prize:{winner.id}",
                source_type="draw_winner_prize",
                source_id=winner.id,
                reason=f"Prize rank {winner.rank} for draw {draw.id}",
                commit=False,
            )
            if credited.journal_group is None:
                raise ConflictError("DRAW_PRIZE_LEDGER_FAILED", "Prize credit did not create a journal group")
            award = PrizeAward(
                id=uuid4(),
                draw_id=draw.id,
                winner_id=winner.id,
                wallet_id=wallet_id,
                journal_group_id=credited.journal_group.id,
                idempotency_record_id=credited.journal_group.idempotency_record_id,
                amount_paise=winner.prize_paise,
                currency="INR",
                credited_by_user_id=actor_user_id,
                credited_at=now,
            )
            self.session.add(award)
            self.audit.record(
                actor_user_id=actor_user_id,
                entity_type="prize_award",
                entity_id=award.id,
                action="DRAW_PRIZE_CREDITED",
                before_state=None,
                after_state=self._award_state(award),
            )

        before_draw = self._draw_state(draw)
        draw.status = DrawStatus.PRIZES_POSTED
        draw.prizes_posted_by_user_id = actor_user_id
        draw.prizes_posted_at = now
        record = self.idempotency.record(
            actor_scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint,
            resource_type="draw",
            resource_id=draw.id,
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="draw",
            entity_id=draw.id,
            action="DRAW_PRIZES_POSTED",
            before_state=before_draw,
            after_state=self._draw_state(draw),
        )
        await self.session.flush()
        response = await self._admin_response(series, draw)
        record.response_payload = response
        await self._finish(commit=commit)
        return DrawMutationResult(draw, series, response, False)

    async def publish_results(
        self,
        *,
        series_id: UUID,
        actor_user_id: UUID,
        idempotency_key: str,
        commit: bool,
    ) -> DrawMutationResult:
        """Publish a fully paid result and create durable in-app winner notices."""

        await self._require_admin(actor_user_id)
        scope = f"admin:{actor_user_id}:draw.publish"
        request = {"action": "publish", "series_id": series_id}
        request_fingerprint = fingerprint(request)
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay(replay)

        series = await self._locked_series(series_id)
        draw = await self._locked_draw_for_series(series.id)
        if series.status != TicketSeriesStatus.DRAWN or draw.status != DrawStatus.PRIZES_POSTED:
            raise ConflictError("DRAW_PUBLISH_NOT_READY", "Results can publish only after all prizes are posted")
        winners = await list_winners(self.session, draw.id, for_update=True)
        awards = await list_prize_awards(self.session, draw.id, for_update=True)
        if len(winners) != len(self._prizes_from_draw(draw)) or len(awards) != len(winners):
            raise ConflictError("DRAW_PRIZES_INCOMPLETE", "Every winner requires one posted prize before publication")
        winner_by_id = {winner.id: winner for winner in winners}
        if set(award.winner_id for award in awards) != set(winner_by_id) or any(
            award.amount_paise != winner_by_id[award.winner_id].prize_paise for award in awards
        ):
            raise ConflictError("DRAW_PRIZES_INCOMPLETE", "Prize award evidence does not match the winners")
        if await list_draw_notifications(self.session, draw.id, for_update=True):
            raise ConflictError("DRAW_NOTIFICATION_STATE_INVALID", "Winner notifications already exist before publication")
        ticket_ids = [winner.ticket_id for winner in winners]
        ticket_by_id = {
            ticket.id: ticket
            for ticket in list(await self.session.scalars(select(Ticket).where(Ticket.id.in_(ticket_ids)).with_for_update()))
        }
        if len(ticket_by_id) != len(winners):
            raise ConflictError("DRAW_WINNERS_INCOMPLETE", "A winner ticket is unavailable for result publication")

        now = self._now()
        before_draw = self._draw_state(draw)
        before_series = TicketSeriesService.snapshot(series)
        for winner in winners:
            ticket = ticket_by_id[winner.ticket_id]
            notification = DrawNotification(
                id=uuid4(),
                draw_id=draw.id,
                winner_id=winner.id,
                user_id=winner.owner_user_id,
                event_type=RESULT_NOTIFICATION_EVENT,
                payload=canonical_payload(
                    {
                        "series_id": series.id,
                        "draw_id": draw.id,
                        "winner_id": winner.id,
                        "rank": winner.rank,
                        "title": winner.title,
                        "prize_paise": winner.prize_paise,
                        "ticket_serial_number": ticket.serial_number,
                    }
                ),
            )
            self.session.add(notification)
            self.audit.record(
                actor_user_id=actor_user_id,
                entity_type="draw_notification",
                entity_id=notification.id,
                action="DRAW_WINNER_NOTIFICATION_CREATED",
                before_state=None,
                after_state=self._notification_state(notification),
            )
        draw.status = DrawStatus.RESULT_PUBLISHED
        draw.published_by_user_id = actor_user_id
        draw.published_at = now
        require_transition(
            current=series.status,
            target=TicketSeriesStatus.RESULT_PUBLISHED,
            transitions=SERIES_TRANSITIONS,
            resource="Ticket series",
        )
        series.status = TicketSeriesStatus.RESULT_PUBLISHED
        record = self.idempotency.record(
            actor_scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint,
            resource_type="draw",
            resource_id=draw.id,
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="draw",
            entity_id=draw.id,
            action="DRAW_RESULT_PUBLISHED",
            before_state=before_draw,
            after_state=self._draw_state(draw),
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="ticket_series",
            entity_id=series.id,
            action="TICKET_SERIES_RESULT_PUBLISHED",
            before_state=before_series,
            after_state=TicketSeriesService.snapshot(series),
        )
        await self.session.flush()
        response = await self._admin_response(series, draw)
        record.response_payload = response
        await self._finish(commit=commit)
        return DrawMutationResult(draw, series, response, False)

    async def admin_snapshot(self, *, series_id: UUID, actor_user_id: UUID) -> dict[str, object]:
        """Return a private operational view without leaking an unreleased seed."""

        await self._require_admin(actor_user_id)
        series = await self._series(series_id)
        draw = await get_draw_by_series(self.session, series.id)
        if draw is None:
            raise ValidationError("DRAW_NOT_FOUND", "This ticket series has no draw")
        return await self._admin_response(series, draw)

    async def public_snapshot(self, *, series: TicketSeries, draw: Draw) -> dict[str, object]:
        """Return only a published, independently reproducible result."""

        if draw.status != DrawStatus.RESULT_PUBLISHED or series.status != TicketSeriesStatus.RESULT_PUBLISHED:
            raise ValidationError("RESULT_NOT_PUBLISHED", "The requested result has not been published")
        if (
            not draw.seed_reveal
            or not draw.eligible_tickets_digest
            or not draw.result_digest
            or draw.drawn_at is None
            or draw.published_at is None
        ):
            raise ConflictError("DRAW_RESULT_INCOMPLETE", "Published draw evidence is incomplete")
        self._require_supported_algorithm(draw)
        if not hmac.compare_digest(sha256(draw.seed_reveal.encode("utf-8")).hexdigest(), draw.seed_commitment):
            raise ConflictError("DRAW_RESULT_INCOMPLETE", "Published seed evidence does not match its commitment")
        entry_count = await count_draw_entries(self.session, draw.id)
        if entry_count != draw.eligible_ticket_count:
            raise ConflictError("DRAW_RESULT_INCOMPLETE", "Published candidate evidence is incomplete")
        winners = await list_winners(self.session, draw.id)
        winner_entries = await list_draw_entries_for_tickets(
            self.session,
            draw.id,
            [winner.ticket_id for winner in winners],
        )
        entry_by_ticket_id = {entry.ticket_id: entry for entry in winner_entries}
        if len(entry_by_ticket_id) != len(winners):
            raise ConflictError("DRAW_RESULT_INCOMPLETE", "Published winner entry evidence is incomplete")
        public_winners = [
            self._public_winner_state(winner, entry_by_ticket_id[winner.ticket_id].serial_number)
            for winner in winners
        ]
        prizes = self._prizes_from_draw(draw)
        expected_result_digest = self._result_digest(draw=draw, prizes=prizes, winners=public_winners)
        if not hmac.compare_digest(expected_result_digest, draw.result_digest):
            raise ConflictError("DRAW_RESULT_INCOMPLETE", "Published result evidence did not verify")
        response = {
            "id": draw.id,
            "series_id": series.id,
            "series_name": series.name,
            "algorithm_version": draw.algorithm_version,
            "seed_commitment": draw.seed_commitment,
            "seed_reveal": draw.seed_reveal,
            "eligible_ticket_count": draw.eligible_ticket_count,
            "eligible_tickets_digest": draw.eligible_tickets_digest,
            "prize_snapshot": prizes,
            "result_digest": draw.result_digest,
            "drawn_at": self._utc(draw.drawn_at),
            "published_at": self._utc(draw.published_at),
            "winners": public_winners,
        }
        return canonical_payload(response)

    async def public_candidate_page(
        self,
        *,
        series: TicketSeries,
        draw: Draw,
        after_serial_number: int,
        limit: int,
    ) -> dict[str, object]:
        """Expose the published candidate manifest in bounded, reproducible pages."""

        if isinstance(after_serial_number, bool) or not isinstance(after_serial_number, int) or after_serial_number < 0:
            raise ValidationError(
                "INVALID_RESULT_CURSOR",
                "after_serial_number must be a non-negative integer",
            )
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1_000:
            raise ValidationError("INVALID_RESULT_PAGE_SIZE", "limit must be an integer between 1 and 1000")

        # This checks the published state, commitment/reveal pair, winner proof,
        # and bounded candidate count without reloading the entire manifest.
        public_result = await self.public_snapshot(series=series, draw=draw)
        serial_numbers = await list_draw_entry_serial_numbers(
            self.session,
            draw.id,
            after_serial_number=after_serial_number,
            limit=limit + 1,
        )
        has_more = len(serial_numbers) > limit
        page = serial_numbers[:limit]
        return canonical_payload(
            {
                "series_id": series.id,
                "eligible_ticket_count": public_result["eligible_ticket_count"],
                "eligible_tickets_digest": public_result["eligible_tickets_digest"],
                "ticket_serial_numbers": page,
                "next_after_serial_number": page[-1] if has_more and page else None,
            }
        )

    async def _admin_response(self, series: TicketSeries, draw: Draw) -> dict[str, object]:
        winners = await list_winners(self.session, draw.id)
        awards = await list_prize_awards(self.session, draw.id)
        winner_entries = await list_draw_entries_for_tickets(
            self.session,
            draw.id,
            [winner.ticket_id for winner in winners],
        )
        entry_by_ticket_id = {entry.ticket_id: entry for entry in winner_entries}
        if len(entry_by_ticket_id) != len(winners):
            raise ConflictError("DRAW_WINNERS_INCOMPLETE", "A winner entry is unavailable")
        award_by_winner = {award.winner_id: award for award in awards}
        response = {
            "id": draw.id,
            "series_id": series.id,
            "series_name": series.name,
            "series_status": series.status.value,
            "status": draw.status,
            "algorithm_version": draw.algorithm_version,
            "seed_commitment": draw.seed_commitment,
            "seed_revealed": draw.seed_reveal is not None,
            "eligible_ticket_count": draw.eligible_ticket_count,
            "eligible_tickets_digest": draw.eligible_tickets_digest,
            "prize_snapshot": self._prizes_from_draw(draw) if draw.prize_snapshot is not None else [],
            "result_digest": draw.result_digest,
            "committed_at": self._utc(draw.committed_at),
            "closed_at": self._optional_utc(draw.closed_at),
            "drawn_at": self._optional_utc(draw.drawn_at),
            "prizes_posted_at": self._optional_utc(draw.prizes_posted_at),
            "published_at": self._optional_utc(draw.published_at),
            "winners": [
                self._admin_winner_state(
                    winner,
                    ticket_serial_number=entry_by_ticket_id[winner.ticket_id].serial_number,
                    award=award_by_winner.get(winner.id),
                )
                for winner in winners
            ],
            # The legacy close endpoint returns this nested catalog snapshot;
            # new draw endpoints use the top-level operational fields.
            "series": TicketSeriesService.snapshot(series),
        }
        return canonical_payload(response)

    async def _locked_draw_for_series(self, series_id: UUID) -> Draw:
        draw = await get_draw_by_series(self.session, series_id, for_update=True)
        if draw is None:
            raise ValidationError("DRAW_NOT_FOUND", "This ticket series has no draw")
        return draw

    async def _locked_series(self, series_id: UUID) -> TicketSeries:
        series = await get_ticket_series(self.session, series_id, for_update=True)
        if series is None:
            raise ValidationError("UNKNOWN_TICKET_SERIES", "Ticket series does not exist")
        return series

    async def _series(self, series_id: UUID) -> TicketSeries:
        series = await get_ticket_series(self.session, series_id)
        if series is None:
            raise ValidationError("UNKNOWN_TICKET_SERIES", "Ticket series does not exist")
        return series

    async def _replay(self, record: IdempotencyRecord) -> DrawMutationResult:
        if record.resource_id is None or not isinstance(record.response_payload, dict):
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior draw mutation cannot be recovered")
        draw = await get_draw(self.session, record.resource_id)
        if draw is None:
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior draw mutation has no draw")
        series = await self._series(draw.series_id)
        return DrawMutationResult(draw, series, dict(record.response_payload), True)

    async def _require_admin(self, actor_user_id: UUID) -> User:
        actor = await self.session.scalar(
            select(User).options(selectinload(User.roles)).where(User.id == actor_user_id).with_for_update()
        )
        if actor is None or not actor.is_active or RoleName.ADMIN.value not in {role.name for role in actor.roles}:
            raise AuthorizationError("Draw and result changes require an administrator")
        return actor

    async def _finish(self, *, commit: bool) -> None:
        await self.session.flush()
        if commit:
            await self.session.commit()

    @classmethod
    def _validate_commitment(cls, value: object) -> str:
        if not isinstance(value, str) or len(value) != 64 or value != value.lower():
            raise ValidationError("INVALID_DRAW_COMMITMENT", "seed_commitment must be a lowercase SHA-256 digest")
        if any(character not in "0123456789abcdef" for character in value):
            raise ValidationError("INVALID_DRAW_COMMITMENT", "seed_commitment must be hexadecimal")
        return value

    @classmethod
    def _validate_reveal(cls, value: object) -> str:
        if (
            not isinstance(value, str)
            or not 16 <= len(value) <= 512
            or any(not 0x21 <= ord(character) <= 0x7E for character in value)
        ):
            raise ValidationError(
                "INVALID_DRAW_REVEAL",
                "seed_reveal must contain 16-512 visible ASCII characters",
            )
        return value

    @staticmethod
    def _require_supported_algorithm(draw: Draw) -> None:
        if draw.algorithm_version != DRAW_ALGORITHM_VERSION:
            raise ConflictError("DRAW_ALGORITHM_UNSUPPORTED", "The draw uses an unsupported selection algorithm")

    @classmethod
    def _prize_snapshot_from_series(cls, prizes: list[TicketSeriesPrize]) -> list[dict[str, object]]:
        if not prizes:
            raise ConflictError("DRAW_PRIZES_REQUIRED", "A draw requires at least one configured prize")
        snapshot = [
            {"rank": prize.rank, "title": prize.title, "prize_paise": prize.prize_paise}
            for prize in sorted(prizes, key=lambda value: value.rank)
        ]
        if len({int(prize["rank"]) for prize in snapshot}) != len(snapshot):
            raise ConflictError("DRAW_PRIZES_INVALID", "Frozen prize ranks must be unique")
        return snapshot

    @classmethod
    def _prizes_from_draw(cls, draw: Draw) -> list[dict[str, object]]:
        raw = draw.prize_snapshot
        if not isinstance(raw, list) or not raw:
            raise ConflictError("DRAW_PRIZES_INVALID", "The frozen prize snapshot is missing")
        prizes: list[dict[str, object]] = []
        ranks: set[int] = set()
        for value in raw:
            if not isinstance(value, dict):
                raise ConflictError("DRAW_PRIZES_INVALID", "The frozen prize snapshot is invalid")
            rank = value.get("rank")
            title = value.get("title")
            amount = value.get("prize_paise")
            if (
                isinstance(rank, bool)
                or not isinstance(rank, int)
                or rank <= 0
                or not isinstance(title, str)
                or not title.strip()
                or len(title) > 100
                or isinstance(amount, bool)
                or not isinstance(amount, int)
                or amount <= 0
                or rank in ranks
            ):
                raise ConflictError("DRAW_PRIZES_INVALID", "The frozen prize snapshot is invalid")
            ranks.add(rank)
            prizes.append({"rank": rank, "title": title, "prize_paise": amount})
        return sorted(prizes, key=lambda value: int(value["rank"]))

    @classmethod
    def _ticket_candidate_state(cls, ticket: Ticket) -> dict[str, object]:
        return {
            "ticket_id": ticket.id,
            "ticket_serial_number": ticket.serial_number,
            "owner_user_id": ticket.owner_user_id,
        }

    @classmethod
    def _entry_candidate_state(cls, entry: DrawEntry) -> dict[str, object]:
        return {
            "ticket_id": entry.ticket_id,
            "ticket_serial_number": entry.serial_number,
            "owner_user_id": entry.owner_user_id,
        }

    @classmethod
    def _candidate_digest(
        cls, *, series_id: UUID, algorithm_version: str, entries: list[dict[str, object]]
    ) -> str:
        serial_numbers = [entry["ticket_serial_number"] for entry in entries]
        return fingerprint(
            {
                "algorithm_version": algorithm_version,
                "series_id": series_id,
                "eligible_ticket_serial_numbers": serial_numbers,
            }
        )

    @classmethod
    def _select_index(
        cls,
        *,
        seed_reveal: str,
        eligible_tickets_digest: str,
        rank: int,
        population_size: int,
    ) -> tuple[int, int]:
        """Map SHA-256 output uniformly without modulo bias."""

        if population_size <= 0:
            raise ConflictError("DRAW_SELECTION_INVALID", "No eligible ticket remains for this prize rank")
        upper_bound = 1 << 256
        rejection_limit = upper_bound - (upper_bound % population_size)
        counter = 0
        while True:
            material = "\x1f".join(
                (
                    DRAW_ALGORITHM_VERSION,
                    seed_reveal,
                    eligible_tickets_digest,
                    str(rank),
                    str(counter),
                )
            )
            value = int.from_bytes(sha256(material.encode("utf-8")).digest(), byteorder="big")
            if value < rejection_limit:
                return value % population_size, counter
            counter += 1

    @classmethod
    def _result_digest(
        cls, *, draw: Draw, prizes: list[dict[str, object]], winners: list[dict[str, object]]
    ) -> str:
        if draw.seed_reveal is None or draw.eligible_tickets_digest is None:
            raise ConflictError("DRAW_RESULT_INCOMPLETE", "A result digest requires seed and candidate evidence")
        return fingerprint(
            {
                "algorithm_version": draw.algorithm_version,
                "seed_commitment": draw.seed_commitment,
                "seed_reveal": draw.seed_reveal,
                "eligible_ticket_count": draw.eligible_ticket_count,
                "eligible_tickets_digest": draw.eligible_tickets_digest,
                "prize_snapshot": prizes,
                "winners": winners,
            }
        )

    @classmethod
    def _draw_state(cls, draw: Draw) -> dict[str, object]:
        return {
            "id": draw.id,
            "series_id": draw.series_id,
            "status": draw.status,
            "algorithm_version": draw.algorithm_version,
            "seed_commitment": draw.seed_commitment,
            "seed_revealed": draw.seed_reveal is not None,
            "eligible_ticket_count": draw.eligible_ticket_count,
            "eligible_tickets_digest": draw.eligible_tickets_digest,
            "prize_snapshot": draw.prize_snapshot,
            "result_digest": draw.result_digest,
            "committed_by_user_id": draw.committed_by_user_id,
            "committed_at": cls._utc(draw.committed_at).isoformat(),
            "closed_by_user_id": draw.closed_by_user_id,
            "closed_at": cls._optional_datetime_text(draw.closed_at),
            "drawn_by_user_id": draw.drawn_by_user_id,
            "drawn_at": cls._optional_datetime_text(draw.drawn_at),
            "prizes_posted_by_user_id": draw.prizes_posted_by_user_id,
            "prizes_posted_at": cls._optional_datetime_text(draw.prizes_posted_at),
            "published_by_user_id": draw.published_by_user_id,
            "published_at": cls._optional_datetime_text(draw.published_at),
        }

    @classmethod
    def _ticket_state(cls, ticket: Ticket) -> dict[str, object]:
        return {
            "id": ticket.id,
            "series_id": ticket.series_id,
            "serial_number": ticket.serial_number,
            "owner_user_id": ticket.owner_user_id,
            "status": ticket.status,
            "is_winner": ticket.is_winner,
            "prize_paise": ticket.prize_paise,
        }

    @classmethod
    def _winner_state(cls, winner: Winner, *, ticket_serial_number: int) -> dict[str, object]:
        return {
            "id": winner.id,
            "draw_id": winner.draw_id,
            "ticket_id": winner.ticket_id,
            "ticket_serial_number": ticket_serial_number,
            "owner_user_id": winner.owner_user_id,
            "rank": winner.rank,
            "title": winner.title,
            "prize_paise": winner.prize_paise,
            "selection_counter": winner.selection_counter,
            "selection_digest": winner.selection_digest,
        }

    @classmethod
    def _public_winner_state(cls, winner: Winner, ticket_serial_number: int) -> dict[str, object]:
        return {
            "rank": winner.rank,
            "title": winner.title,
            "prize_paise": winner.prize_paise,
            "ticket_serial_number": ticket_serial_number,
            "selection_counter": winner.selection_counter,
            "selection_digest": winner.selection_digest,
        }

    @classmethod
    def _admin_winner_state(
        cls, winner: Winner, *, ticket_serial_number: int, award: PrizeAward | None
    ) -> dict[str, object]:
        response = cls._winner_state(winner, ticket_serial_number=ticket_serial_number)
        response.update(
            {
                "prize_awarded": award is not None,
                "prize_award_id": award.id if award is not None else None,
                "journal_group_id": award.journal_group_id if award is not None else None,
                "credited_at": cls._optional_utc(award.credited_at) if award is not None else None,
            }
        )
        return response

    @classmethod
    def _award_state(cls, award: PrizeAward) -> dict[str, object]:
        return {
            "id": award.id,
            "draw_id": award.draw_id,
            "winner_id": award.winner_id,
            "wallet_id": award.wallet_id,
            "journal_group_id": award.journal_group_id,
            "idempotency_record_id": award.idempotency_record_id,
            "amount_paise": award.amount_paise,
            "currency": award.currency,
            "credited_by_user_id": award.credited_by_user_id,
            "credited_at": cls._utc(award.credited_at).isoformat(),
        }

    @classmethod
    def _notification_state(cls, notification: DrawNotification) -> dict[str, object]:
        return {
            "id": notification.id,
            "draw_id": notification.draw_id,
            "winner_id": notification.winner_id,
            "user_id": notification.user_id,
            "event_type": notification.event_type,
            "payload": notification.payload,
            "delivered_at": cls._optional_datetime_text(notification.delivered_at),
        }

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    @classmethod
    def _optional_utc(cls, value: datetime | None) -> datetime | None:
        return cls._utc(value) if value is not None else None

    @classmethod
    def _optional_datetime_text(cls, value: datetime | None) -> str | None:
        return cls._utc(value).isoformat() if value is not None else None
