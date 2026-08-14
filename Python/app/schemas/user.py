"""Request/response schemas for user resources.

Validation lives here, at the edge of the system, so that by the time a payload
reaches a service function it is already known-good and the service can be
written without defensive checks.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.config import settings
from app.models.user import UserRole

_HAS_LETTER = re.compile(r"[A-Za-z]")
_HAS_DIGIT = re.compile(r"\d")

PasswordStr = Annotated[
    str,
    Field(
        min_length=8,  # hard floor; the real minimum comes from settings below
        max_length=128,
        description="Must contain at least one letter and one digit.",
        examples=["Str0ngPassphrase"],
    ),
]


def validate_password_strength(value: str) -> str:
    """Enforce the password policy in one place, reused by every schema."""
    if len(value) < settings.PASSWORD_MIN_LENGTH:
        raise ValueError(
            f"Password must be at least {settings.PASSWORD_MIN_LENGTH} characters long."
        )
    if not _HAS_LETTER.search(value):
        raise ValueError("Password must contain at least one letter.")
    if not _HAS_DIGIT.search(value):
        raise ValueError("Password must contain at least one digit.")
    if value.strip() != value:
        raise ValueError("Password must not begin or end with whitespace.")
    return value


def normalise_email(value: Any) -> Any:
    """Lower-case and trim so uniqueness is effectively case-insensitive."""
    return value.strip().lower() if isinstance(value, str) else value


class UserBase(BaseModel):
    email: EmailStr = Field(examples=["jane.doe@example.com"])
    full_name: str | None = Field(default=None, max_length=255, examples=["Jane Doe"])

    _normalise_email = field_validator("email", mode="before")(normalise_email)

    @field_validator("full_name")
    @classmethod
    def _strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class UserCreate(UserBase):
    """Registration payload."""

    password: PasswordStr

    _check_password = field_validator("password")(validate_password_strength)


class UserUpdate(BaseModel):
    """Partial update of the caller's own profile. Every field is optional."""

    email: EmailStr | None = None
    full_name: str | None = Field(default=None, max_length=255)

    _normalise_email = field_validator("email", mode="before")(normalise_email)


class PasswordChange(BaseModel):
    """Changing a password requires proving knowledge of the current one."""

    current_password: str = Field(min_length=1, max_length=128)
    new_password: PasswordStr

    _check_password = field_validator("new_password")(validate_password_strength)


class UserRead(BaseModel):
    """Public representation of a user - never includes the password hash."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    full_name: str | None
    role: UserRole
    is_active: bool
    created_at: datetime
    updated_at: datetime


class UserAdminUpdate(BaseModel):
    """Admin-only mutations that a user may not perform on themselves."""

    role: UserRole | None = None
    is_active: bool | None = None
