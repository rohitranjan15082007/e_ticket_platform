"""Registration, login and current-identity endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, status

from app.dependencies import SessionDependency, get_current_user
from app.models.user import User
from app.schemas.auth import AccessTokenResponse, LoginRequest, LoginResponse, RegisterRequest, UserResponse
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["authentication"])


def serialize_user(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        is_active=user.is_active,
        roles=sorted(role.name for role in user.roles),
    )


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest,
    session: SessionDependency,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=255)],
) -> UserResponse:
    user = await AuthService(session).register(payload, idempotency_key)
    return serialize_user(user)


@router.post("/login", response_model=LoginResponse)
async def login(payload: LoginRequest, session: SessionDependency) -> LoginResponse:
    result = await AuthService(session).login(payload)
    return LoginResponse(
        token=AccessTokenResponse(access_token=result.access_token),
        user=serialize_user(result.user),
    )


@router.get("/me", response_model=UserResponse)
async def current_identity(current_user: Annotated[User, Depends(get_current_user)]) -> UserResponse:
    return serialize_user(current_user)
