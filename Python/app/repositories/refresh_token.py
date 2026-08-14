"""Refresh-token session data access."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import utcnow
from app.models.refresh_token import RefreshToken, RevocationReason


async def create(
    db: AsyncSession,
    *,
    user_id: int,
    jti: str,
    expires_at: datetime,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> RefreshToken:
    token = RefreshToken(
        user_id=user_id,
        jti=jti,
        expires_at=expires_at,
        # Long UA strings are common; the column is 255 so truncate rather than
        # let the database reject the insert.
        user_agent=user_agent[:255] if user_agent else None,
        ip_address=ip_address[:45] if ip_address else None,
    )
    db.add(token)
    await db.flush()
    return token


async def get_by_jti(db: AsyncSession, jti: str) -> RefreshToken | None:
    return await db.scalar(select(RefreshToken).where(RefreshToken.jti == jti))


async def revoke(
    db: AsyncSession, token: RefreshToken, *, reason: RevocationReason
) -> None:
    """Revoke one session. The reason drives reuse detection - see the model."""
    if token.revoked_at is None:
        token.revoked_at = utcnow()
        token.revoked_reason = reason
        await db.flush()


async def revoke_all_for_user(
    db: AsyncSession, user_id: int, *, reason: RevocationReason
) -> int:
    """Revoke every live session for a user. Returns the number revoked.

    Issued as one bulk UPDATE rather than a load-then-loop: this runs on the
    "my account was compromised" path where the session count is unbounded.
    """
    stmt = (
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow(), revoked_reason=reason)
    )
    result = await db.execute(stmt)
    await db.flush()
    return int(result.rowcount or 0)


async def list_active_for_user(db: AsyncSession, user_id: int) -> list[RefreshToken]:
    stmt = (
        select(RefreshToken)
        .where(
            RefreshToken.user_id == user_id,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > utcnow(),
        )
        .order_by(RefreshToken.created_at.desc())
    )
    return list((await db.scalars(stmt)).all())


async def delete_expired(db: AsyncSession) -> int:
    """Housekeeping: drop rows that can no longer be redeemed.

    Called on startup. Without this the table grows forever, since every login
    and every refresh adds a row.
    """
    from sqlalchemy import delete

    stmt = delete(RefreshToken).where(RefreshToken.expires_at <= utcnow())
    result = await db.execute(stmt)
    return int(result.rowcount or 0)
