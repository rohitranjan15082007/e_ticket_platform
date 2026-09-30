"""Declarative model base and common timestamp/UUID columns."""

from datetime import datetime
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class used by every SQLAlchemy model."""


class UUIDPrimaryKeyMixin:
    id: Mapped[UUID] = mapped_column(sa.Uuid(as_uuid=True), primary_key=True, default=uuid4)


class CreatedAtMixin:
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class TimestampMixin(CreatedAtMixin):
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
    )
