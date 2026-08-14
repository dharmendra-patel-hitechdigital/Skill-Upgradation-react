"""Authentication endpoints: register, login, refresh, logout, sessions."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.security import OAuth2PasswordRequestForm

from app.api.deps import ClientInfo, CurrentUser, DBSession
from app.repositories import refresh_token as token_repo
from app.schemas.common import ErrorResponse, Message
from app.schemas.token import (
    LoginResponse,
    LogoutRequest,
    RefreshRequest,
    SessionRead,
    TokenPair,
)
from app.schemas.user import UserCreate, UserRead
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post(
    "/register",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new account",
    responses={
        409: {"model": ErrorResponse, "description": "Email already registered"},
        422: {"model": ErrorResponse, "description": "Password policy not met"},
    },
)
async def register(payload: UserCreate, db: DBSession) -> UserRead:
    """Create an account.

    The **first** account created on a fresh installation is granted the `admin`
    role automatically; every subsequent account is a regular `user`.

    Passwords must be at least 10 characters and contain a letter and a digit.
    They are stored as a bcrypt hash and never logged.
    """
    user = await auth_service.register_user(db, payload)
    await db.commit()
    return UserRead.model_validate(user)


@router.post(
    "/login",
    response_model=LoginResponse,
    summary="Log in and obtain tokens",
    responses={
        401: {"model": ErrorResponse, "description": "Incorrect email or password"},
        403: {"model": ErrorResponse, "description": "Account deactivated"},
    },
)
async def login(
    db: DBSession,
    client: ClientInfo,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
) -> LoginResponse:
    """Exchange credentials for an access + refresh token pair.

    This is an OAuth2 *password flow* form submission
    (`application/x-www-form-urlencoded`), so the field is named **`username`** -
    put the email address there.

    Use the returned `access_token` as `Authorization: Bearer <token>`. It is
    short-lived; call `/auth/refresh` with the `refresh_token` to get a new pair
    without asking the user to log in again.
    """
    user_agent, ip_address = client
    user = await auth_service.authenticate(
        db, email=form_data.username, password=form_data.password
    )
    tokens = await auth_service.issue_tokens(
        db, user, user_agent=user_agent, ip_address=ip_address
    )
    await db.commit()

    return LoginResponse(
        **tokens.model_dump(), user=UserRead.model_validate(user)
    )


@router.post(
    "/refresh",
    response_model=TokenPair,
    summary="Rotate a refresh token",
    responses={401: {"model": ErrorResponse, "description": "Invalid or reused token"}},
)
async def refresh(
    payload: RefreshRequest, db: DBSession, client: ClientInfo
) -> TokenPair:
    """Exchange a refresh token for a **new pair**.

    Refresh tokens are single-use. The one you send is revoked and a new one is
    returned, so always store the new value.

    If a token that has already been redeemed is presented again, that is treated
    as a possible theft: **every** session for the account is revoked and this
    call fails with 401.
    """
    user_agent, ip_address = client
    tokens = await auth_service.rotate_tokens(
        db, payload.refresh_token, user_agent=user_agent, ip_address=ip_address
    )
    await db.commit()
    return tokens


@router.post("/logout", response_model=Message, summary="Log out")
async def logout(payload: LogoutRequest, db: DBSession) -> Message:
    """Revoke a refresh token, ending that session.

    Set `all_sessions: true` to sign out of every device.

    Always succeeds, even for a token that was already invalid - the desired end
    state ("this token no longer works") is reached either way.

    Note that the *access* token remains cryptographically valid until it expires
    (minutes). Clients should discard it on logout.
    """
    revoked = await auth_service.logout(
        db, payload.refresh_token, all_sessions=payload.all_sessions
    )
    await db.commit()
    return Message(detail=f"Logged out. {revoked} session(s) revoked.")


@router.get(
    "/sessions",
    response_model=list[SessionRead],
    summary="List your active sessions",
)
async def list_sessions(db: DBSession, current_user: CurrentUser) -> list[SessionRead]:
    """Show every live (unrevoked, unexpired) session for the current account."""
    sessions = await token_repo.list_active_for_user(db, current_user.id)
    return [SessionRead.model_validate(session) for session in sessions]
