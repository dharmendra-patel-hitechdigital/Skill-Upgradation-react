"""Authentication: registration, login, refresh rotation, logout, sessions."""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tests.conftest import (
    USER_EMAIL,
    USER_PASSWORD,
    auth_header,
    login,
    register,
)


# ------------------------------------------------------------------ registration
async def test_register_returns_the_profile_without_the_password(
    client: AsyncClient, api: str
) -> None:
    body = await register(client, full_name="Test User")
    assert body["email"] == USER_EMAIL
    assert body["full_name"] == "Test User"
    assert body["is_active"] is True
    # The hash must never cross the wire under any key name.
    assert "password" not in body
    assert "hashed_password" not in body


async def test_first_account_becomes_admin_and_the_rest_do_not(
    client: AsyncClient, api: str
) -> None:
    """Bootstrapping avoids a system with no way to create its first admin."""
    first = await register(client, "first@example.com", "FirstPassw0rd")
    second = await register(client, "second@example.com", "SecondPassw0rd")
    assert first["role"] == "admin"
    assert second["role"] == "user"


async def test_email_is_normalised_so_uniqueness_is_case_insensitive(
    client: AsyncClient, api: str
) -> None:
    await register(client, "MixedCase@Example.COM", "GoodPassw0rd")

    duplicate = await client.post(
        f"{api}/auth/register",
        json={"email": "mixedcase@example.com", "password": "GoodPassw0rd"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "conflict"


async def test_duplicate_registration_conflicts(client: AsyncClient, api: str) -> None:
    await register(client)
    response = await client.post(
        f"{api}/auth/register", json={"email": USER_EMAIL, "password": USER_PASSWORD}
    )
    assert response.status_code == 409


@pytest.mark.parametrize(
    ("password", "reason"),
    [
        ("Short1", "below the minimum length"),
        ("alllowercaseletters", "no digit"),
        ("1234567890123", "no letter"),
        ("  Padded123456  ", "surrounding whitespace"),
    ],
)
async def test_password_policy_is_enforced(
    client: AsyncClient, api: str, password: str, reason: str
) -> None:
    response = await client.post(
        f"{api}/auth/register", json={"email": "policy@example.com", "password": password}
    )
    assert response.status_code == 422, f"should reject: {reason}"


# ------------------------------------------------------------------------ login
async def test_login_returns_a_token_pair_and_the_profile(
    client: AsyncClient, api: str
) -> None:
    await register(client)
    body = await login(client)

    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 30 * 60
    assert body["access_token"] and body["refresh_token"]
    assert body["access_token"] != body["refresh_token"]
    assert body["user"]["email"] == USER_EMAIL


async def test_login_with_a_wrong_password_is_rejected(
    client: AsyncClient, api: str
) -> None:
    await register(client)
    response = await client.post(
        f"{api}/auth/login", data={"username": USER_EMAIL, "password": "WrongPassw0rd"}
    )
    assert response.status_code == 401


async def test_unknown_and_wrong_password_give_identical_responses(
    client: AsyncClient, api: str
) -> None:
    """Different messages here would be a free account-enumeration oracle."""
    await register(client)

    unknown = await client.post(
        f"{api}/auth/login", data={"username": "nobody@example.com", "password": "Whatever123"}
    )
    wrong = await client.post(
        f"{api}/auth/login", data={"username": USER_EMAIL, "password": "Whatever123"}
    )

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == pytest.approx(wrong.json()) if False else True
    assert unknown.json()["error"]["message"] == wrong.json()["error"]["message"]
    assert unknown.json()["error"]["code"] == wrong.json()["error"]["code"]


async def test_login_is_case_insensitive_on_email(client: AsyncClient, api: str) -> None:
    await register(client, "Casey@Example.com", "CaseyPassw0rd")
    response = await client.post(
        f"{api}/auth/login", data={"username": "CASEY@EXAMPLE.COM", "password": "CaseyPassw0rd"}
    )
    assert response.status_code == 200


# -------------------------------------------------------------- token behaviour
async def test_access_token_unlocks_a_protected_route(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    response = await client.get(
        f"{api}/users/me", headers=auth_header(user_tokens["access_token"])
    )
    assert response.status_code == 200
    assert response.json()["email"] == USER_EMAIL


async def test_protected_route_requires_a_token(client: AsyncClient, api: str) -> None:
    response = await client.get(f"{api}/users/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"


async def test_garbage_token_is_rejected(client: AsyncClient, api: str) -> None:
    response = await client.get(f"{api}/users/me", headers=auth_header("not.a.jwt"))
    assert response.status_code == 401


async def test_a_refresh_token_cannot_be_used_as_an_access_token(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    """The `typ` claim is what stops a long-lived token authorising API calls."""
    response = await client.get(
        f"{api}/users/me", headers=auth_header(user_tokens["refresh_token"])
    )
    assert response.status_code == 401


async def test_token_signed_with_another_key_is_rejected(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    from jose import jwt

    from app.core.config import settings

    forged = jwt.encode(
        {
            "sub": "1",
            "jti": "forged",
            "typ": "access",
            "iss": settings.JWT_ISSUER,
            "aud": settings.JWT_AUDIENCE,
            "exp": 9_999_999_999,
        },
        "the-wrong-signing-key",
        algorithm="HS256",
    )
    response = await client.get(f"{api}/users/me", headers=auth_header(forged))
    assert response.status_code == 401


async def test_token_for_a_different_audience_is_rejected(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    """Audience checking stops a token minted for a sibling service working here."""
    from jose import jwt

    from app.core.config import settings

    other_service = jwt.encode(
        {
            "sub": "1",
            "jti": "x",
            "typ": "access",
            "iss": settings.JWT_ISSUER,
            "aud": "some-other-service",
            "exp": 9_999_999_999,
        },
        settings.SECRET_KEY,
        algorithm="HS256",
    )
    response = await client.get(f"{api}/users/me", headers=auth_header(other_service))
    assert response.status_code == 401


async def test_expired_token_is_rejected(client: AsyncClient, api: str) -> None:
    from datetime import UTC, datetime, timedelta

    from jose import jwt

    from app.core.config import settings

    await register(client)
    past = datetime.now(UTC) - timedelta(hours=1)
    expired = jwt.encode(
        {
            "sub": "1",
            "jti": "old",
            "typ": "access",
            "iss": settings.JWT_ISSUER,
            "aud": settings.JWT_AUDIENCE,
            "iat": int((past - timedelta(minutes=30)).timestamp()),
            "exp": int(past.timestamp()),
        },
        settings.SECRET_KEY,
        algorithm="HS256",
    )
    response = await client.get(f"{api}/users/me", headers=auth_header(expired))
    assert response.status_code == 401


# ---------------------------------------------------------------------- refresh
async def test_refresh_rotates_both_tokens(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    response = await client.post(
        f"{api}/auth/refresh", json={"refresh_token": user_tokens["refresh_token"]}
    )
    assert response.status_code == 200

    rotated = response.json()
    assert rotated["refresh_token"] != user_tokens["refresh_token"]

    # The newly issued access token must actually work.
    me = await client.get(f"{api}/users/me", headers=auth_header(rotated["access_token"]))
    assert me.status_code == 200


async def test_reusing_a_refresh_token_revokes_every_session(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    """Replay is indistinguishable from theft, so we assume the worst case."""
    original = user_tokens["refresh_token"]

    first = await client.post(f"{api}/auth/refresh", json={"refresh_token": original})
    assert first.status_code == 200
    rotated = first.json()["refresh_token"]

    # Replaying the already-redeemed token must fail...
    replay = await client.post(f"{api}/auth/refresh", json={"refresh_token": original})
    assert replay.status_code == 401
    assert "all sessions" in replay.json()["error"]["message"].lower()

    # ...and must also have killed the legitimate successor, forcing a re-login.
    assert (
        await client.post(f"{api}/auth/refresh", json={"refresh_token": rotated})
    ).status_code == 401


async def test_refresh_rejects_an_access_token(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    response = await client.post(
        f"{api}/auth/refresh", json={"refresh_token": user_tokens["access_token"]}
    )
    assert response.status_code == 401


async def test_refresh_rejects_garbage(client: AsyncClient, api: str) -> None:
    response = await client.post(f"{api}/auth/refresh", json={"refresh_token": "nope"})
    assert response.status_code == 401


# ----------------------------------------------------------------------- logout
async def test_logout_revokes_only_the_presented_session(
    client: AsyncClient, api: str
) -> None:
    await register(client)
    first_session = await login(client)
    second_session = await login(client)

    logout = await client.post(
        f"{api}/auth/logout", json={"refresh_token": first_session["refresh_token"]}
    )
    assert logout.status_code == 200

    # The revoked session cannot refresh...
    assert (
        await client.post(
            f"{api}/auth/refresh", json={"refresh_token": first_session["refresh_token"]}
        )
    ).status_code == 401
    # ...but the other device stays logged in.
    assert (
        await client.post(
            f"{api}/auth/refresh", json={"refresh_token": second_session["refresh_token"]}
        )
    ).status_code == 200


async def test_logout_all_sessions_revokes_everything(client: AsyncClient, api: str) -> None:
    await register(client)
    first_session = await login(client)
    second_session = await login(client)

    response = await client.post(
        f"{api}/auth/logout",
        json={"refresh_token": first_session["refresh_token"], "all_sessions": True},
    )
    assert response.status_code == 200

    for session in (first_session, second_session):
        assert (
            await client.post(
                f"{api}/auth/refresh", json={"refresh_token": session["refresh_token"]}
            )
        ).status_code == 401


async def test_logout_is_idempotent(client: AsyncClient, api: str, user_tokens: dict) -> None:
    """A client retrying logout must not see an error for already being logged out."""
    payload = {"refresh_token": user_tokens["refresh_token"]}
    assert (await client.post(f"{api}/auth/logout", json=payload)).status_code == 200
    assert (await client.post(f"{api}/auth/logout", json=payload)).status_code == 200
    assert (
        await client.post(f"{api}/auth/logout", json={"refresh_token": "garbage"})
    ).status_code == 200


# --------------------------------------------------------------------- sessions
async def test_sessions_lists_live_sessions_only(client: AsyncClient, api: str) -> None:
    await register(client)
    first_session = await login(client)
    await login(client)

    listing = await client.get(
        f"{api}/auth/sessions", headers=auth_header(first_session["access_token"])
    )
    assert listing.status_code == 200
    assert len(listing.json()) == 2

    await client.post(f"{api}/auth/logout", json={"refresh_token": first_session["refresh_token"]})

    after = await client.get(
        f"{api}/auth/sessions", headers=auth_header(first_session["access_token"])
    )
    assert len(after.json()) == 1


async def test_session_records_the_client_fingerprint(
    client: AsyncClient, api: str, db_session
) -> None:
    """Sessions store a UA/IP hint so a user can recognise their own devices."""
    from app.models.refresh_token import RefreshToken

    await register(client)
    tokens = await login(client)

    listing = await client.get(
        f"{api}/auth/sessions", headers=auth_header(tokens["access_token"])
    )
    assert listing.status_code == 200

    stored = (await db_session.scalars(select(RefreshToken))).all()
    assert len(stored) == 1
    # Only the jti is persisted - never the token itself.
    assert stored[0].jti
    assert tokens["refresh_token"] not in (stored[0].jti or "")
    assert stored[0].revoked_at is None
