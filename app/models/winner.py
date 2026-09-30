"""Immutable candidate, winner, award, and result-publication records.

The draw workflow stores a snapshot of eligible tickets before selection. A
published result is therefore reproducible from the commitment, reveal,
snapshot digest, and selection transcript rather than an administrator-supplied
list of preferred winners.
"""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin


class DrawStatus(StrEnum):
    """Durable stages of the one draw owned by a ticket series."""

    COMMITTED = "COMMITTED"
    SALES_CLOSED = "SALES_CLOSED"
    DRAWN = "DRAWN"
    PRIZES_POSTED = "PRIZES_POSTED"
    RESULT_PUBLISHED = "RESULT_PUBLISHED"


class Draw(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The evidence envelope for one series draw."""

    __tablename__ = "draws"
    __table_args__ = (
        sa.CheckConstraint("eligible_ticket_count >= 0", name="ck_draw_eligible_ticket_count_nonnegative"),
        sa.CheckConstraint("length(seed_commitment) = 64", name="ck_draw_seed_commitment_sha256"),
        sa.UniqueConstraint("series_id", name="uq_draw_series"),
    )

    series_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("ticket_series.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[DrawStatus] = mapped_column(
        sa.Enum(DrawStatus, name="draw_status"),
        nullable=False,
        default=DrawStatus.COMMITTED,
        server_default=DrawStatus.COMMITTED.value,
        index=True,
    )
    algorithm_version: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    seed_commitment: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    seed_reveal: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    eligible_ticket_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0, server_default="0")
    eligible_tickets_digest: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    prize_snapshot: Mapped[list[dict[str, object]] | None] = mapped_column(sa.JSON, nullable=True)
    result_digest: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    committed_by_user_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    committed_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    closed_by_user_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    drawn_by_user_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    drawn_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    prizes_posted_by_user_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    prizes_posted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    published_by_user_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    published_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class DrawEntry(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """A locked snapshot of an allocated ticket eligible for one draw."""

    __tablename__ = "draw_entries"
    __table_args__ = (
        sa.CheckConstraint("serial_number > 0", name="ck_draw_entry_serial_positive"),
        sa.UniqueConstraint("draw_id", "ticket_id", name="uq_draw_entry_ticket"),
        sa.UniqueConstraint("draw_id", "serial_number", name="uq_draw_entry_serial"),
    )

    draw_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("draws.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    ticket_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("tickets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    serial_number: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    owner_user_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )


class Winner(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One deterministically selected ticket for one frozen prize rank."""

    __tablename__ = "winners"
    __table_args__ = (
        sa.CheckConstraint("rank > 0", name="ck_winner_rank_positive"),
        sa.CheckConstraint("prize_paise > 0", name="ck_winner_prize_positive"),
        sa.CheckConstraint("selection_counter >= 0", name="ck_winner_selection_counter_nonnegative"),
        sa.UniqueConstraint("draw_id", "rank", name="uq_winner_draw_rank"),
        sa.UniqueConstraint("draw_id", "ticket_id", name="uq_winner_draw_ticket"),
        sa.UniqueConstraint("draw_id", "id", name="uq_winner_draw_id"),
        sa.ForeignKeyConstraint(
            ["draw_id", "ticket_id"],
            ["draw_entries.draw_id", "draw_entries.ticket_id"],
            name="fk_winner_draw_entry",
            ondelete="RESTRICT",
        ),
    )

    draw_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("draws.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    ticket_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("tickets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    owner_user_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    rank: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    title: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    prize_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    selection_counter: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    selection_digest: Mapped[str] = mapped_column(sa.String(64), nullable=False)


class PrizeAward(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """The one ledger-backed credit that fulfils a selected prize."""

    __tablename__ = "prize_awards"
    __table_args__ = (
        sa.CheckConstraint("amount_paise > 0", name="ck_prize_award_amount_positive"),
        sa.CheckConstraint("currency = 'INR'", name="ck_prize_award_currency_inr"),
        sa.UniqueConstraint("winner_id", name="uq_prize_award_winner"),
        sa.UniqueConstraint("journal_group_id", name="uq_prize_award_journal_group"),
        sa.UniqueConstraint("idempotency_record_id", name="uq_prize_award_idempotency"),
        sa.ForeignKeyConstraint(
            ["draw_id", "winner_id"],
            ["winners.draw_id", "winners.id"],
            name="fk_prize_award_draw_winner",
            ondelete="RESTRICT",
        ),
    )

    draw_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("draws.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    winner_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("winners.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    wallet_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("wallets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    journal_group_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("journal_groups.id", ondelete="RESTRICT"), nullable=False
    )
    idempotency_record_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("idempotency_records.id", ondelete="RESTRICT"), nullable=False
    )
    amount_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False, default="INR", server_default="INR")
    credited_by_user_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    credited_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class DrawNotification(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """A durable in-app winner notification, separate from P2P delivery."""

    __tablename__ = "draw_notifications"
    __table_args__ = (
        sa.UniqueConstraint("draw_id", "winner_id", "event_type", name="uq_draw_notification_event"),
        sa.ForeignKeyConstraint(
            ["draw_id", "winner_id"],
            ["winners.draw_id", "winners.id"],
            name="fk_draw_notification_draw_winner",
            ondelete="RESTRICT",
        ),
    )

    draw_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("draws.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    winner_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("winners.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    user_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(sa.JSON, nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
