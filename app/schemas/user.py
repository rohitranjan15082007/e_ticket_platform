"""Minimal administrator user read projection; never expose credential data."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class AdminUserSummaryResponse(BaseModel):
    id: UUID
    email_hint: str
    full_name: str | None
    is_active: bool
    is_verified: bool
    roles: list[str]
    created_at: datetime


class ProfileResponse(BaseModel):
    id: UUID
    email: str
    full_name: str | None
    is_active: bool
    is_verified: bool
    roles: list[str]
    created_at: datetime
