"""Profile management, password changes, and admin user operations."""
from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import (
    USER_EMAIL,
    USER_PASSWORD,
    auth_header,
    login,
    register,
)


# --------------------------------------------------------------------- profile
async def test_me_returns_the_caller(client: AsyncClient, api: str, user_tokens: dict) -> None:
    response = await client.get(f"{api}/users/me", headers=auth_header(user_tokens["access_token"]))
    assert response.status_code == 200

    body = response.json()
    assert body["email"] == USER_EMAIL
    assert body["role"] == "user"


async def test_patch_updates_only_the_supplied_fields(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    headers = auth_header(user_tokens["access_token"])

    response = await client.patch(
        f"{api}/users/me", json={"full_name": "Renamed Person"}, headers=headers
    )
    assert response.status_code == 200
    assert response.json()["full_name"] == "Renamed Person"
    # Omitted fields must be left alone, not nulled.
    assert response.json()["email"] == USER_EMAIL


async def test_patch_can_change_the_email(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    response = await client.patch(
        f"{api}/users/me",
        json={"email": "Renamed@Example.COM"},
        headers=auth_header(user_tokens["access_token"]),
    )
    assert response.status_code == 200
    assert response.json()["email"] == "renamed@example.com"


async def test_patch_rejects_an_email_owned_by_someone_else(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    response = await client.patch(
        f"{api}/users/me",
        json={"email": "admin@example.com"},
        headers=auth_header(user_tokens["access_token"]),
    )
    assert response.status_code == 409


async def test_a_user_cannot_promote_themselves(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    """`role` is not part of UserUpdate, so it must be ignored, not applied."""
    response = await client.patch(
        f"{api}/users/me",
        json={"full_name": "Sneaky", "role": "admin"},
        headers=auth_header(user_tokens["access_token"]),
    )
    assert response.status_code == 200
    assert response.json()["role"] == "user"


# -------------------------------------------------------------- password change
async def test_password_change_works_and_signs_out_every_session(
    client: AsyncClient, api: str
) -> None:
    await register(client)
    first_session = await login(client)
    second_session = await login(client)

    response = await client.post(
        f"{api}/users/me/password",
        json={"current_password": USER_PASSWORD, "new_password": "BrandNewPass99"},
        headers=auth_header(first_session["access_token"]),
    )
    assert response.status_code == 200

    # Every session is dead - a leaked old password must not leave a way back in.
    for session in (first_session, second_session):
        refresh = await client.post(
            f"{api}/auth/refresh", json={"refresh_token": session["refresh_token"]}
        )
        assert refresh.status_code == 401

    assert (
        await client.post(
            f"{api}/auth/login", data={"username": USER_EMAIL, "password": USER_PASSWORD}
        )
    ).status_code == 401
    assert (
        await client.post(
            f"{api}/auth/login", data={"username": USER_EMAIL, "password": "BrandNewPass99"}
        )
    ).status_code == 200


async def test_password_change_requires_the_current_password(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    response = await client.post(
        f"{api}/users/me/password",
        json={"current_password": "WrongCurrent1", "new_password": "BrandNewPass99"},
        headers=auth_header(user_tokens["access_token"]),
    )
    assert response.status_code == 401


async def test_password_change_rejects_reusing_the_same_password(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    response = await client.post(
        f"{api}/users/me/password",
        json={"current_password": USER_PASSWORD, "new_password": USER_PASSWORD},
        headers=auth_header(user_tokens["access_token"]),
    )
    assert response.status_code == 409


async def test_new_password_must_meet_the_policy(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    response = await client.post(
        f"{api}/users/me/password",
        json={"current_password": USER_PASSWORD, "new_password": "weak"},
        headers=auth_header(user_tokens["access_token"]),
    )
    assert response.status_code == 422


async def test_revoke_all_sessions_endpoint(client: AsyncClient, api: str) -> None:
    await register(client)
    session = await login(client)
    await login(client)

    response = await client.delete(
        f"{api}/users/me/sessions", headers=auth_header(session["access_token"])
    )
    assert response.status_code == 200
    assert "2 session(s) revoked" in response.json()["detail"]


# ----------------------------------------------------------------------- admin
async def test_admin_can_list_users(client: AsyncClient, api: str, admin_tokens: dict) -> None:
    response = await client.get(
        f"{api}/users", headers=auth_header(admin_tokens["access_token"])
    )
    assert response.status_code == 200

    body = response.json()
    assert body["meta"]["total"] == 2
    assert body["meta"]["page"] == 1
    assert len(body["items"]) == 2


async def test_a_regular_user_cannot_list_users(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    response = await client.get(
        f"{api}/users", headers=auth_header(user_tokens["access_token"])
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


async def test_pagination_metadata_is_correct(
    client: AsyncClient, api: str, admin_tokens: dict
) -> None:
    response = await client.get(
        f"{api}/users?page=1&page_size=1", headers=auth_header(admin_tokens["access_token"])
    )
    meta = response.json()["meta"]
    assert (meta["total"], meta["total_pages"], meta["has_next"], meta["has_previous"]) == (
        2,
        2,
        True,
        False,
    )


async def test_page_size_is_capped(client: AsyncClient, api: str, admin_tokens: dict) -> None:
    """An unbounded page size is an easy way to exhaust the database."""
    response = await client.get(
        f"{api}/users?page_size=5000", headers=auth_header(admin_tokens["access_token"])
    )
    assert response.status_code == 422


async def test_admin_can_deactivate_a_user_which_takes_effect_at_once(
    client: AsyncClient, api: str, admin_tokens: dict
) -> None:
    """Active status is read from the database, so it applies before token expiry."""
    victim = await login(client, USER_EMAIL, USER_PASSWORD)

    assert (
        await client.get(f"{api}/users/me", headers=auth_header(victim["access_token"]))
    ).status_code == 200

    users = (
        await client.get(f"{api}/users", headers=auth_header(admin_tokens["access_token"]))
    ).json()["items"]
    victim_id = next(user["id"] for user in users if user["email"] == USER_EMAIL)

    response = await client.patch(
        f"{api}/users/{victim_id}",
        json={"is_active": False},
        headers=auth_header(admin_tokens["access_token"]),
    )
    assert response.status_code == 200
    assert response.json()["is_active"] is False

    # The still-unexpired access token stops working immediately...
    assert (
        await client.get(f"{api}/users/me", headers=auth_header(victim["access_token"]))
    ).status_code == 403
    # ...and their sessions cannot refresh a way back in.
    assert (
        await client.post(
            f"{api}/auth/refresh", json={"refresh_token": victim["refresh_token"]}
        )
    ).status_code == 401


async def test_admin_can_grant_the_admin_role(
    client: AsyncClient, api: str, admin_tokens: dict
) -> None:
    users = (
        await client.get(f"{api}/users", headers=auth_header(admin_tokens["access_token"]))
    ).json()["items"]
    target_id = next(user["id"] for user in users if user["email"] == USER_EMAIL)

    response = await client.patch(
        f"{api}/users/{target_id}",
        json={"role": "admin"},
        headers=auth_header(admin_tokens["access_token"]),
    )
    assert response.status_code == 200
    assert response.json()["role"] == "admin"


async def test_admin_cannot_change_their_own_role(
    client: AsyncClient, api: str, admin_tokens: dict
) -> None:
    """Guard against the last administrator locking themselves out."""
    me = (
        await client.get(f"{api}/users/me", headers=auth_header(admin_tokens["access_token"]))
    ).json()

    response = await client.patch(
        f"{api}/users/{me['id']}",
        json={"is_active": False},
        headers=auth_header(admin_tokens["access_token"]),
    )
    assert response.status_code == 403


async def test_admin_cannot_delete_themselves(
    client: AsyncClient, api: str, admin_tokens: dict
) -> None:
    me = (
        await client.get(f"{api}/users/me", headers=auth_header(admin_tokens["access_token"]))
    ).json()
    response = await client.delete(
        f"{api}/users/{me['id']}", headers=auth_header(admin_tokens["access_token"])
    )
    assert response.status_code == 403


async def test_admin_delete_removes_the_user(
    client: AsyncClient, api: str, admin_tokens: dict
) -> None:
    headers = auth_header(admin_tokens["access_token"])
    users = (await client.get(f"{api}/users", headers=headers)).json()["items"]
    target_id = next(user["id"] for user in users if user["email"] == USER_EMAIL)

    assert (await client.delete(f"{api}/users/{target_id}", headers=headers)).status_code == 204
    assert (await client.get(f"{api}/users", headers=headers)).json()["meta"]["total"] == 1
    assert (await client.delete(f"{api}/users/{target_id}", headers=headers)).status_code == 404


async def test_admin_update_of_a_missing_user_is_404(
    client: AsyncClient, api: str, admin_tokens: dict
) -> None:
    response = await client.patch(
        f"{api}/users/9999",
        json={"is_active": False},
        headers=auth_header(admin_tokens["access_token"]),
    )
    assert response.status_code == 404
