"""Schemas for token issuing, refresh, logout, and session listing."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.user import UserRead


class TokenPair(BaseModel):
    """Issued on login and on every refresh (tokens are rotated, not reused)."""

    access_token: str = Field(examples=["eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."])
    refresh_token: str = Field(examples=["eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."])
    token_type: str = "bearer"
    expires_in: int = Field(
        description="Seconds until the access token expires.", examples=[1800]
    )


class LoginResponse(TokenPair):
    """Login also returns the profile, saving the client an extra round-trip."""

    user: UserRead


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class LogoutRequest(BaseModel):
    """Logout revokes the presented refresh token.

    ``all_sessions=True`` revokes every other live session too - the "sign out
    everywhere" action you want after losing a device.
    """

    refresh_token: str = Field(min_length=1)
    all_sessions: bool = False


class SessionRead(BaseModel):
    """One live refresh-token session belonging to the caller."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    expires_at: datetime
    user_agent: str | None
    ip_address: str | None
