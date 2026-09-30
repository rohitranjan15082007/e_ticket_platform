"""Authentication request and response contracts."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=12, max_length=256)
    full_name: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized.count("@") != 1 or normalized.startswith("@") or normalized.endswith("@"):
            raise ValueError("A valid email address is required")
        local, domain = normalized.split("@")
        if not local or "." not in domain:
            raise ValueError("A valid email address is required")
        return normalized


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.strip().lower()


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    full_name: str | None
    is_active: bool
    roles: list[str]


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class LoginResponse(BaseModel):
    token: AccessTokenResponse
    user: UserResponse
