"""Append-only audit records for state and financial changes."""

from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class AuditLog(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "audit_logs"

    actor_user_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    entity_type: Mapped[str] = mapped_column(sa.String(100), nullable=False, index=True)
    entity_id: Mapped[UUID] = mapped_column(sa.Uuid(as_uuid=True), nullable=False, index=True)
    action: Mapped[str] = mapped_column(sa.String(100), nullable=False, index=True)
    before_state: Mapped[dict[str, object] | None] = mapped_column(sa.JSON, nullable=True)
    after_state: Mapped[dict[str, object] | None] = mapped_column(sa.JSON, nullable=True)
    reason: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)
