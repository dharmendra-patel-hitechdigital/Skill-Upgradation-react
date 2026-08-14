"""Persisted refresh-token registry.

Access tokens stay stateless (fast, no DB hit). Refresh tokens are tracked so
the system gains three things a purely stateless design cannot provide:

1. **Real logout** - revoke the stored ``jti`` and the session is dead
   immediately, even though the JWT itself is still cryptographically valid.
2. **Rotation with reuse detection** - redeeming a refresh token revokes it and
   issues a new one. If an already-revoked token is presented again, that is a
   strong signal the token was stolen, so every session for that user is
   revoked.
3. **Session visibility** - the user can see and end their active sessions.

Only the token's ``jti`` is stored, never the token string itself.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin, UtcDateTime, portable_enum, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.models.user import User


class RevocationReason(StrEnum):
    """Why a refresh token was revoked.

    This distinction is load-bearing, not bookkeeping. Presenting a token that
    was **consumed by rotation** means someone kept a copy they should have
    discarded - the signature of a stolen token, so every session is killed.
    Presenting one that was revoked by **logout** is a mundane client mistake
    (a stale tab, a retried request) and must only fail that one call.
    Conflating the two would let any user log out and then lock every one of
    their own devices out by accident.
    """

    ROTATED = "rotated"
    LOGOUT = "logout"
    PASSWORD_CHANGE = "password_change"
    SECURITY = "security"
    ADMIN = "admin"


class RefreshToken(TimestampMixin, Base):
    __tablename__ = "refresh_tokens"
    __table_args__ = (
        # Supports "revoke all live sessions for this user" without a scan.
        Index("ix_refresh_tokens_user_revoked", "user_id", "revoked_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    jti: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    revoked_reason: Mapped[RevocationReason | None] = mapped_column(
        portable_enum(RevocationReason), nullable=True
    )

    # Coarse session fingerprint - useful for the session list and for spotting
    # a refresh replayed from a different client.
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)

    user: Mapped[User] = relationship(back_populates="refresh_tokens", lazy="raise")

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def was_consumed_by_rotation(self) -> bool:
        """True when this token was spent on a refresh - so a replay is suspicious."""
        return self.revoked_reason is RevocationReason.ROTATED

    @property
    def is_expired(self) -> bool:
        return self.expires_at <= utcnow()

    @property
    def is_usable(self) -> bool:
        return not self.is_revoked and not self.is_expired
