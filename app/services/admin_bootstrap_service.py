"""One-time, audited administrator bootstrap for an empty admin roster."""

import hashlib
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import RoleName
from app.core.security import hash_password
from app.exceptions import ConflictError
from app.models.audit_log import AuditLog
from app.models.user import IdempotencyRecord, Role, User, user_roles
from app.repositories.user_repository import get_role_by_name, get_user_by_email, get_user_by_id
from app.schemas.auth import RegisterRequest


@dataclass(slots=True)
class AdminBootstrapResult:
    user_id: UUID
    replayed: bool


class AdminBootstrapService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_first_admin(
        self, *, email: str, password: str, idempotency_key: str, commit: bool,
    ) -> AdminBootstrapResult:
        if not 8 <= len(idempotency_key) <= 255:
            raise ValueError("Idempotency key must be 8-255 characters")
        identity = RegisterRequest(email=email, password=password)
        scope = "bootstrap:first-admin"
        fingerprint = hashlib.sha256(
            f"first-admin-v1\x1f{identity.email}\x1f{password}".encode("utf-8")
        ).hexdigest()
        if self.session.bind and self.session.bind.dialect.name == "postgresql":
            # Serialize concurrent bootstrap processes even if the admin role is not seeded yet.
            await self.session.execute(text("SELECT pg_advisory_xact_lock(50145201871431)"))

        replay = await self.session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.actor_scope == scope,
                IdempotencyRecord.idempotency_key == idempotency_key,
            )
        )
        if replay is not None:
            if replay.request_fingerprint != fingerprint or replay.resource_id is None:
                raise ConflictError("IDEMPOTENCY_CONFLICT", "Bootstrap key was used with different data")
            user = await get_user_by_id(self.session, replay.resource_id)
            if user is None:
                raise ConflictError("BOOTSTRAP_INCOMPLETE", "Prior bootstrap user cannot be recovered")
            return AdminBootstrapResult(user_id=user.id, replayed=True)

        existing_admin = await self.session.scalar(
            select(User.id).join(user_roles, User.id == user_roles.c.user_id)
            .join(Role, Role.id == user_roles.c.role_id)
            .where(Role.name == RoleName.ADMIN.value).limit(1)
        )
        if existing_admin is not None:
            raise ConflictError("ADMIN_ALREADY_EXISTS", "First-admin bootstrap is already closed")
        if await get_user_by_email(self.session, identity.email):
            raise ConflictError("EMAIL_ALREADY_REGISTERED", "An account already uses this email")

        roles: list[Role] = []
        for name, description in (
            (RoleName.USER.value, "Default platform user"),
            (RoleName.ADMIN.value, "Platform administrator"),
        ):
            role = await get_role_by_name(self.session, name)
            if role is None:
                role = Role(name=name, description=description)
                self.session.add(role)
                await self.session.flush()
            roles.append(role)

        user = User(
            email=identity.email, password_hash=hash_password(password),
            full_name=None, is_active=True, roles=roles,
        )
        self.session.add(user)
        await self.session.flush()
        self.session.add_all([
            IdempotencyRecord(
                actor_scope=scope, idempotency_key=idempotency_key,
                request_fingerprint=fingerprint, resource_type="user",
                resource_id=user.id, status_code=201,
            ),
            AuditLog(
                actor_user_id=None, entity_type="user", entity_id=user.id,
                action="FIRST_ADMIN_BOOTSTRAPPED", before_state=None,
                after_state={"roles": [RoleName.USER.value, RoleName.ADMIN.value]},
                reason="Explicit first-admin maintenance command",
            ),
        ])
        if commit:
            await self.session.commit()
        else:
            await self.session.flush()
        return AdminBootstrapResult(user_id=user.id, replayed=False)
