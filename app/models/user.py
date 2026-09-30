"""Identity, roles and idempotency persistence models."""

from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

user_roles = sa.Table(
    "user_roles",
    Base.metadata,
    sa.Column("user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("role_id", sa.Uuid(as_uuid=True), sa.ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
)


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(sa.String(320), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    full_name: Mapped[str | None] = mapped_column(sa.String(200), nullable=True)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    is_verified: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    roles: Mapped[list["Role"]] = relationship(secondary=user_roles, back_populates="users", lazy="selectin")


class Role(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "roles"

    name: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True, index=True)
    description: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    users: Mapped[list[User]] = relationship(secondary=user_roles, back_populates="roles", lazy="selectin")


class IdempotencyRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Persists the resource created by a retried authenticated mutation."""

    __tablename__ = "idempotency_records"
    __table_args__ = (
        sa.UniqueConstraint("actor_scope", "idempotency_key", name="uq_idempotency_scope_key"),
    )

    actor_scope: Mapped[str] = mapped_column(sa.String(400), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    resource_id: Mapped[UUID | None] = mapped_column(sa.Uuid(as_uuid=True), nullable=True)
    status_code: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False)
    response_payload: Mapped[dict[str, object] | None] = mapped_column(sa.JSON, nullable=True)
