"""User profile endpoints, plus admin user management."""
from __future__ import annotations

from fastapi import APIRouter, Path, status

from app.api.deps import AdminUser, CurrentUser, DBSession, Pagination
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from app.models.refresh_token import RevocationReason
from app.repositories import refresh_token as token_repo
from app.repositories import user as user_repo
from app.schemas.common import ErrorResponse, Message, Page
from app.schemas.user import (
    PasswordChange,
    UserAdminUpdate,
    UserRead,
    UserUpdate,
)
from app.services import auth_service

router = APIRouter(prefix="/users", tags=["Users"])


@router.get("/me", response_model=UserRead, summary="Get your profile")
async def read_me(current_user: CurrentUser) -> UserRead:
    """Return the authenticated user's profile."""
    return UserRead.model_validate(current_user)


@router.patch(
    "/me",
    response_model=UserRead,
    summary="Update your profile",
    responses={409: {"model": ErrorResponse, "description": "Email already in use"}},
)
async def update_me(
    payload: UserUpdate, db: DBSession, current_user: CurrentUser
) -> UserRead:
    """Partially update your own profile.

    `PATCH` semantics: omitted fields are left unchanged. Sending
    `{"full_name": null}` explicitly clears the name.

    Role and active status cannot be changed here - that is an admin operation.
    """
    # Only worth a uniqueness query when the email is actually changing.
    if (
        payload.email
        and str(payload.email) != current_user.email
        and await user_repo.email_exists(
            db, str(payload.email), exclude_id=current_user.id
        )
    ):
        raise ConflictError("An account with this email address already exists.")

    user = await user_repo.update_profile(db, current_user, payload)
    await db.commit()
    return UserRead.model_validate(user)


@router.post(
    "/me/password",
    response_model=Message,
    summary="Change your password",
    responses={
        401: {"model": ErrorResponse, "description": "Current password incorrect"},
        409: {"model": ErrorResponse, "description": "New password matches the old one"},
    },
)
async def change_password(
    payload: PasswordChange, db: DBSession, current_user: CurrentUser
) -> Message:
    """Change your password.

    Requires the current password. On success **all sessions are signed out**,
    including this one - log in again with the new password to obtain fresh
    tokens.
    """
    await auth_service.change_password(
        db,
        current_user,
        current_password=payload.current_password,
        new_password=payload.new_password,
    )
    await db.commit()
    return Message(
        detail="Password updated. All sessions have been signed out; please log in again."
    )


@router.delete(
    "/me/sessions",
    response_model=Message,
    summary="Sign out of all devices",
)
async def revoke_all_sessions(db: DBSession, current_user: CurrentUser) -> Message:
    """Revoke every refresh token for your account."""
    revoked = await token_repo.revoke_all_for_user(
        db, current_user.id, reason=RevocationReason.LOGOUT
    )
    await db.commit()
    return Message(detail=f"{revoked} session(s) revoked.")


# --------------------------------------------------------------------- admin
@router.get(
    "",
    response_model=Page[UserRead],
    summary="List all users (admin)",
    responses={403: {"model": ErrorResponse, "description": "Admin role required"}},
)
async def list_users(
    db: DBSession, pagination: Pagination, _admin: AdminUser
) -> Page[UserRead]:
    """Paginated list of every account. Requires the `admin` role."""
    users, total = await user_repo.list_users(
        db, offset=pagination.offset, limit=pagination.limit
    )
    return Page.build(
        [UserRead.model_validate(user) for user in users],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.patch(
    "/{user_id}",
    response_model=UserRead,
    summary="Update a user's role or status (admin)",
    responses={
        403: {"model": ErrorResponse, "description": "Admin role required"},
        404: {"model": ErrorResponse, "description": "User not found"},
    },
)
async def admin_update_user(
    payload: UserAdminUpdate,
    db: DBSession,
    admin: AdminUser,
    user_id: int = Path(ge=1),
) -> UserRead:
    """Grant/revoke admin, or activate/deactivate an account.

    Deactivating a user takes effect on their **next request** (the active-user
    check reads the database, not the token) and additionally revokes all of
    their sessions.

    An admin cannot change their own role or deactivate themselves - that is the
    easiest way to lock the last administrator out of the system.
    """
    if user_id == admin.id:
        raise PermissionDeniedError(
            "You cannot change your own role or status. Ask another administrator."
        )

    user = await user_repo.get_by_id(db, user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} was not found.")

    updated = await user_repo.admin_update(db, user, payload)
    if payload.is_active is False:
        # A deactivated account must not keep refreshing its way back in.
        await token_repo.revoke_all_for_user(
            db, user_id, reason=RevocationReason.ADMIN
        )

    await db.commit()
    return UserRead.model_validate(updated)


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a user (admin)",
    responses={
        403: {"model": ErrorResponse, "description": "Admin role required"},
        404: {"model": ErrorResponse, "description": "User not found"},
    },
)
async def admin_delete_user(
    db: DBSession, admin: AdminUser, user_id: int = Path(ge=1)
) -> None:
    """Delete an account and cascade-delete its documents and sessions.

    Stored files are **not** removed by this call; run a storage reaper for that.
    Deleting rows synchronously while a user might own thousands of blobs would
    make this request unboundedly slow.
    """
    if user_id == admin.id:
        raise PermissionDeniedError("You cannot delete your own account.")

    user = await user_repo.get_by_id(db, user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} was not found.")

    await db.delete(user)
    await db.commit()
