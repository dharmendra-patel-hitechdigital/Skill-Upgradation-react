"""Authentication use cases: registration, login, rotation, logout.

The refresh-rotation logic in :func:`rotate_tokens` is the security-critical part
of this module - see the note on reuse detection there.
"""
from __future__ import annotations

import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import (
    AuthenticationError,
    ConflictError,
    PermissionDeniedError,
)
from app.core.security import (
    REFRESH_TOKEN_TYPE,
    create_access_token,
    create_refresh_token,
    decode_token,
    fake_verify_password,
    verify_password,
)
from app.models.refresh_token import RevocationReason
from app.models.user import User, UserRole
from app.repositories import refresh_token as token_repo
from app.repositories import user as user_repo
from app.schemas.token import TokenPair
from app.schemas.user import UserCreate

logger = logging.getLogger(__name__)

# One generic message for every credential failure. Distinguishing "no such
# user" from "wrong password" hands an attacker a free account-enumeration
# oracle.
_INVALID_CREDENTIALS = "Incorrect email or password."


async def register_user(db: AsyncSession, payload: UserCreate) -> User:
    """Create a user, promoting the very first one to admin.

    Bootstrapping the first account as admin avoids a chicken-and-egg problem
    (no admin exists, so nobody can grant admin) without shipping a default
    password.
    """
    if await user_repo.email_exists(db, str(payload.email)):
        raise ConflictError("An account with this email address already exists.")

    role = UserRole.ADMIN if await user_repo.count(db) == 0 else UserRole.USER

    try:
        user = await user_repo.create(db, payload, role=role)
    except IntegrityError as exc:
        # Two concurrent registrations with the same email: the unique index is
        # the real arbiter, and one of them lands here.
        await db.rollback()
        raise ConflictError("An account with this email address already exists.") from exc

    logger.info("user_registered", extra={"user_id": user.id, "role": role.value})
    return user


async def authenticate(db: AsyncSession, *, email: str, password: str) -> User:
    """Verify credentials and return the user, or raise a generic 401."""
    user = await user_repo.get_by_email(db, email)

    if user is None:
        # Spend the same time as a real bcrypt verification so response latency
        # does not reveal whether the account exists.
        fake_verify_password()
        raise AuthenticationError(_INVALID_CREDENTIALS)

    if not verify_password(password, user.hashed_password):
        raise AuthenticationError(_INVALID_CREDENTIALS)

    if not user.is_active:
        raise PermissionDeniedError(
            "This account has been deactivated. Contact an administrator."
        )

    return user


async def issue_tokens(
    db: AsyncSession,
    user: User,
    *,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> TokenPair:
    """Mint an access/refresh pair and register the refresh token's session."""
    access = create_access_token(user.id, role=user.role.value)
    refresh = create_refresh_token(user.id)

    await token_repo.create(
        db,
        user_id=user.id,
        jti=refresh.jti,
        expires_at=refresh.expires_at,
        user_agent=user_agent,
        ip_address=ip_address,
    )

    return TokenPair(
        access_token=access.token,
        refresh_token=refresh.token,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


async def rotate_tokens(
    db: AsyncSession,
    raw_refresh_token: str,
    *,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> TokenPair:
    """Exchange a refresh token for a fresh pair, invalidating the old one.

    **Reuse detection.** A refresh token is single-use. If one that has already
    been redeemed is presented again, there are only two explanations: a buggy
    client, or an attacker replaying a stolen token. We cannot tell them apart,
    so we assume the worse case and revoke *every* session for that user. The
    legitimate user is forced to log in again; the attacker's copy is dead.
    This is the standard OAuth 2 recommendation for public clients.
    """
    payload = decode_token(raw_refresh_token, expected_type=REFRESH_TOKEN_TYPE)
    if payload is None:
        raise AuthenticationError("The refresh token is invalid or has expired.")

    stored = await token_repo.get_by_jti(db, payload.jti)
    if stored is None:
        # Validly signed but unknown to us - it was pruned after expiry, or the
        # signing key was rotated.
        raise AuthenticationError("This session is no longer valid. Please log in again.")

    if stored.is_revoked:
        # Only a token *spent on a rotation* implies theft. One revoked by logout
        # (or a password change) is a stale client, and treating that as an attack
        # would let a user accidentally sign out all their own devices.
        if not stored.was_consumed_by_rotation:
            raise AuthenticationError(
                "This session has been signed out. Please log in again."
            )

        revoked = await token_repo.revoke_all_for_user(
            db, stored.user_id, reason=RevocationReason.SECURITY
        )
        # Committed here, breaking this module's usual "the caller owns the
        # transaction" rule, and deliberately so: this request is about to fail
        # with a 401, and `get_db` rolls back on an exception. Without an explicit
        # commit the mass revocation - the entire security response to a suspected
        # stolen token - would be silently discarded.
        await db.commit()
        logger.warning(
            "refresh_token_reuse_detected",
            extra={
                "user_id": stored.user_id,
                "jti": payload.jti,
                "sessions_revoked": revoked,
            },
        )
        raise AuthenticationError(
            "This refresh token has already been used. For your security all "
            "sessions have been signed out. Please log in again."
        )

    if stored.is_expired:
        raise AuthenticationError("This session has expired. Please log in again.")

    user = await user_repo.get_by_id(db, stored.user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("This session is no longer valid. Please log in again.")

    # Revoke first, then issue: if anything below fails, the old token is already
    # dead rather than remaining valid alongside a new one.
    await token_repo.revoke(db, stored, reason=RevocationReason.ROTATED)
    return await issue_tokens(db, user, user_agent=user_agent, ip_address=ip_address)


async def logout(
    db: AsyncSession, raw_refresh_token: str, *, all_sessions: bool = False
) -> int:
    """Revoke the presented session, optionally every other one too.

    Returns the number of sessions revoked. Logout is intentionally forgiving: an
    unparseable or already-dead token still yields success, because the caller's
    goal - "this token must not work" - is satisfied either way, and returning an
    error would only encourage clients to retry.
    """
    payload = decode_token(raw_refresh_token, expected_type=REFRESH_TOKEN_TYPE)
    if payload is None:
        return 0

    stored = await token_repo.get_by_jti(db, payload.jti)
    if stored is None:
        return 0

    if all_sessions:
        return await token_repo.revoke_all_for_user(
            db, stored.user_id, reason=RevocationReason.LOGOUT
        )

    if stored.is_revoked:
        return 0
    await token_repo.revoke(db, stored, reason=RevocationReason.LOGOUT)
    return 1


async def change_password(
    db: AsyncSession, user: User, *, current_password: str, new_password: str
) -> None:
    """Change a password and sign out everywhere.

    Revoking all sessions is the point of a password change: if the old one leaked,
    leaving existing refresh tokens alive would let the intruder stay logged in.
    """
    if not verify_password(current_password, user.hashed_password):
        raise AuthenticationError("The current password is incorrect.")
    if verify_password(new_password, user.hashed_password):
        raise ConflictError("The new password must be different from the current one.")

    await user_repo.set_password(db, user, new_password)
    revoked = await token_repo.revoke_all_for_user(
        db, user.id, reason=RevocationReason.PASSWORD_CHANGE
    )
    logger.info(
        "password_changed", extra={"user_id": user.id, "sessions_revoked": revoked}
    )


async def bootstrap_first_admin(db: AsyncSession) -> User | None:
    """Create the configured admin account if the user table is empty.

    Runs on startup and is a no-op unless both ``FIRST_ADMIN_EMAIL`` and
    ``FIRST_ADMIN_PASSWORD`` are set *and* no users exist - so it can never
    silently reset an existing deployment's credentials.
    """
    if not (settings.FIRST_ADMIN_EMAIL and settings.FIRST_ADMIN_PASSWORD):
        return None
    if await user_repo.count(db) > 0:
        return None

    user = await user_repo.create(
        db,
        UserCreate(
            email=settings.FIRST_ADMIN_EMAIL,  # type: ignore[arg-type]
            full_name="Administrator",
            password=settings.FIRST_ADMIN_PASSWORD,
        ),
        role=UserRole.ADMIN,
    )
    logger.warning(
        "bootstrapped_admin_account",
        extra={"email": user.email, "user_id": user.id},
    )
    return user
