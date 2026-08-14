"""User ORM model."""
from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin, portable_enum

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a circular import
    from app.models.document import Document
    from app.models.refresh_token import RefreshToken


class UserRole(StrEnum):
    """Coarse-grained role. Authorisation checks read this, never a user id."""

    USER = "user"
    ADMIN = "admin"


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Stored lower-cased (normalised by the Pydantic schema) so uniqueness is
    # case-insensitive without needing a functional index.
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)

    role: Mapped[UserRole] = mapped_column(
        portable_enum(UserRole),
        default=UserRole.USER,
        server_default=UserRole.USER.value,
        nullable=False,
        index=True,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1", nullable=False
    )

    documents: Mapped[list[Document]] = relationship(
        back_populates="owner",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",  # forces explicit eager loading; no accidental N+1 or
        # MissingGreenlet from a lazy load in async context.
    )
    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )

    @property
    def is_admin(self) -> bool:
        return self.role is UserRole.ADMIN
