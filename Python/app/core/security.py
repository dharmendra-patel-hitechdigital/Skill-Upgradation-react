"""Password hashing and JWT issuing/validation.

Token design
------------
Two token types, both signed with the same key but *not* interchangeable
(the ``typ`` claim is checked on every use):

* **access**  - short-lived (minutes), sent on every request, never stored
  server-side. Carries the user id and role so authorisation needs no DB hit
  for the common path.
* **refresh** - long-lived (days), single-use. Each one carries a unique
  ``jti`` that is persisted; redeeming it revokes that ``jti`` and issues a new
  pair. That gives us real logout and refresh-token-reuse detection, which a
  stateless-only design cannot offer.

Every token also carries ``iss``/``aud`` so a token minted for this service
cannot be replayed against a sibling service that happens to share a key.
"""
from __future__ import annotations

import hmac
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import bcrypt
from jose import JWTError, jwt

from app.core.config import settings

ACCESS_TOKEN_TYPE: Final = "access"
REFRESH_TOKEN_TYPE: Final = "refresh"

# bcrypt only consumes the first 72 bytes of input; truncating explicitly keeps
# behaviour identical across bcrypt versions (some now raise instead).
_BCRYPT_MAX_BYTES: Final = 72

# A pre-computed hash of a throwaway value. Verifying against it lets the login
# endpoint spend the same ~100ms whether or not the email exists, so response
# timing does not leak account existence.
_DUMMY_HASH: Final = bcrypt.hashpw(b"timing-equalizer", bcrypt.gensalt()).decode()


# --------------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    """Hash a plaintext password with bcrypt (per-password random salt)."""
    return bcrypt.hashpw(_prepare(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Constant-time check of a plaintext password against its bcrypt hash."""
    try:
        return bcrypt.checkpw(_prepare(plain_password), hashed_password.encode("utf-8"))
    except ValueError:
        # Malformed/legacy hash in the database - treat as a failed login
        # rather than a 500.
        return False


def fake_verify_password() -> None:
    """Burn one bcrypt verification to equalise timing for unknown accounts."""
    bcrypt.checkpw(b"timing-equalizer", _DUMMY_HASH.encode("utf-8"))


def _prepare(password: str) -> bytes:
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


# ------------------------------------------------------------------------ tokens
@dataclass(frozen=True, slots=True)
class IssuedToken:
    """A signed JWT plus the metadata the caller may need to persist."""

    token: str
    jti: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class TokenPayload:
    """Validated claims from an incoming token."""

    subject: int
    token_type: str
    jti: str
    role: str | None
    issued_at: datetime | None
    expires_at: datetime | None
    raw: dict[str, Any]


def _issue(
    subject: str | int,
    expires_delta: timedelta,
    token_type: str,
    extra_claims: dict[str, Any] | None = None,
) -> IssuedToken:
    now = datetime.now(UTC)
    expires_at = now + expires_delta
    jti = uuid.uuid4().hex
    claims: dict[str, Any] = {
        "sub": str(subject),
        "jti": jti,
        "typ": token_type,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
    }
    if extra_claims:
        claims.update(extra_claims)
    token = jwt.encode(claims, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return IssuedToken(token=token, jti=jti, expires_at=expires_at)


def create_access_token(subject: str | int, *, role: str | None = None) -> IssuedToken:
    return _issue(
        subject,
        timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        ACCESS_TOKEN_TYPE,
        {"role": role} if role else None,
    )


def create_refresh_token(subject: str | int) -> IssuedToken:
    return _issue(
        subject,
        timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        REFRESH_TOKEN_TYPE,
    )


def decode_token(token: str, *, expected_type: str | None = None) -> TokenPayload | None:
    """Verify signature, expiry, issuer, audience and (optionally) token type.

    Returns ``None`` for anything that does not validate - callers turn that
    into a single generic 401 so we never explain *why* a token was rejected.
    """
    try:
        claims = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
            audience=settings.JWT_AUDIENCE,
            issuer=settings.JWT_ISSUER,
        )
    except JWTError:
        return None

    token_type = claims.get("typ")
    if expected_type is not None and not hmac.compare_digest(
        str(token_type), expected_type
    ):
        return None

    subject_raw = claims.get("sub")
    jti = claims.get("jti")
    if subject_raw is None or jti is None:
        return None
    try:
        subject = int(subject_raw)
    except (TypeError, ValueError):
        return None

    return TokenPayload(
        subject=subject,
        token_type=str(token_type),
        jti=str(jti),
        role=claims.get("role"),
        issued_at=_as_datetime(claims.get("iat")),
        expires_at=_as_datetime(claims.get("exp")),
        raw=claims,
    )


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError, OSError):
        return None
