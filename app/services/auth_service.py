"""Authentication business logic; routes contain no authentication decisions."""

import hashlib
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import RoleName
from app.core.security import create_access_token, hash_password, verify_password
from app.exceptions import AuthenticationError, ConflictError
from app.models.user import IdempotencyRecord, Role, User
from app.repositories.user_repository import get_role_by_name, get_user_by_email
from app.schemas.auth import LoginRequest, RegisterRequest


@dataclass(slots=True)
class AuthenticationResult:
    user: User
    access_token: str


class AuthService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def register(self, payload: RegisterRequest, idempotency_key: str) -> User:
        """Create a standard user exactly once for an email-scoped retry key."""

        scope = f"register:{payload.email}"
        fingerprint = self._registration_fingerprint(payload)
        record = await self.session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.actor_scope == scope,
                IdempotencyRecord.idempotency_key == idempotency_key,
            )
        )
        if record is not None:
            if record.request_fingerprint != fingerprint:
                raise ConflictError(
                    "IDEMPOTENCY_CONFLICT", "Idempotency-Key was already used with different data"
                )
            if record.resource_id is None:
                raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior request did not complete safely")
            user = await self.session.get(User, record.resource_id)
            if user is None:
                raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior request cannot be recovered")
            return user

        if await get_user_by_email(self.session, payload.email):
            raise ConflictError("EMAIL_ALREADY_REGISTERED", "This email address is already registered")

        default_role = await get_role_by_name(self.session, RoleName.USER.value)
        if default_role is None:
            default_role = Role(name=RoleName.USER.value, description="Default platform user")
            self.session.add(default_role)
            await self.session.flush()

        user = User(
            email=payload.email,
            password_hash=hash_password(payload.password),
            full_name=payload.full_name.strip() if payload.full_name else None,
            roles=[default_role],
        )
        self.session.add(user)
        await self.session.flush()
        self.session.add(
            IdempotencyRecord(
                actor_scope=scope,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                resource_type="user",
                resource_id=user.id,
                status_code=201,
            )
        )
        await self.session.commit()
        await self.session.refresh(user, attribute_names=["roles"])
        return user

    async def login(self, payload: LoginRequest) -> AuthenticationResult:
        user = await get_user_by_email(self.session, payload.email)
        if user is None or not user.is_active or not verify_password(payload.password, user.password_hash):
            raise AuthenticationError("Incorrect email or password")
        roles = sorted(role.name for role in user.roles)
        return AuthenticationResult(user=user, access_token=create_access_token(str(user.id), roles))

    @staticmethod
    def _registration_fingerprint(payload: RegisterRequest) -> str:
        source = "\x1f".join(("v1", payload.email, payload.full_name or "", payload.password))
        return hashlib.sha256(source.encode("utf-8")).hexdigest()
