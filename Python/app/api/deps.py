"""Shared FastAPI dependencies.

Each dependency is exported as an ``Annotated`` alias (``DBSession``,
``CurrentUser``, ``AdminUser``...). Endpoints then declare
``current_user: CurrentUser`` instead of repeating
``Depends(get_current_active_user)`` everywhere - the auth requirement becomes
part of the signature's type, and it shows up correctly in the OpenAPI schema.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Path, Query, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.exceptions import AuthenticationError, NotFoundError, PermissionDeniedError
from app.core.security import ACCESS_TOKEN_TYPE, decode_token
from app.models.document import Document, DocumentStatus
from app.models.user import User
from app.repositories import document as doc_repo
from app.repositories import user as user_repo
from app.schemas.common import PaginationParams
from app.schemas.document import DocumentFilters, DocumentSortField, SortDirection

# `auto_error=False` so a missing header raises *our* error envelope rather than
# Starlette's bare {"detail": "Not authenticated"} - one error shape everywhere.
oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl=f"{settings.API_V1_PREFIX}/auth/login",
    auto_error=False,
    scheme_name="OAuth2 password (email + password)",
)

DBSession = Annotated[AsyncSession, Depends(get_db)]
OptionalToken = Annotated[str | None, Depends(oauth2_scheme)]


async def get_current_user(db: DBSession, token: OptionalToken) -> User:
    """Resolve the caller from a bearer access token.

    Every rejection returns the same generic 401: telling a caller *why* a token
    failed (expired vs. wrong type vs. unknown user) is information they do not
    need and an attacker does.
    """
    if not token:
        raise AuthenticationError("Not authenticated.")

    payload = decode_token(token, expected_type=ACCESS_TOKEN_TYPE)
    if payload is None:
        raise AuthenticationError()

    user = await user_repo.get_by_id(db, payload.subject)
    if user is None:
        raise AuthenticationError()

    return user


async def get_current_active_user(
    current_user: Annotated[User, Depends(get_current_user)],
) -> User:
    """The default guard: authenticated *and* not deactivated.

    Checked on every request rather than trusted from the token, so deactivating
    an account takes effect immediately instead of at token expiry.
    """
    if not current_user.is_active:
        raise PermissionDeniedError("This account has been deactivated.")
    return current_user


CurrentUser = Annotated[User, Depends(get_current_active_user)]


async def get_current_admin(current_user: CurrentUser) -> User:
    """Require the admin role. Roles are read from the database, not the token."""
    if not current_user.is_admin:
        raise PermissionDeniedError("This action requires administrator privileges.")
    return current_user


AdminUser = Annotated[User, Depends(get_current_admin)]


def get_pagination(
    page: Annotated[int, Query(ge=1, description="1-based page number.")] = 1,
    page_size: Annotated[
        int, Query(ge=1, le=100, description="Records per page (max 100).")
    ] = 20,
) -> PaginationParams:
    return PaginationParams(page=page, page_size=page_size)


Pagination = Annotated[PaginationParams, Depends(get_pagination)]


def get_document_filters(
    status: Annotated[
        DocumentStatus | None, Query(description="Filter by processing status.")
    ] = None,
    document_type: Annotated[
        str | None,
        Query(max_length=64, description="Filter by AI-assigned document type."),
    ] = None,
    search: Annotated[
        str | None,
        Query(max_length=255, description="Case-insensitive filename substring."),
    ] = None,
    owner_email: Annotated[
        str | None,
        Query(
            max_length=255,
            description=(
                "Case-insensitive substring of the uploader's email. Useful to an "
                "administrator reviewing one user's uploads; it cannot widen a "
                "regular user's list, which is already scoped to their own rows."
            ),
        ),
    ] = None,
    sort_by: Annotated[DocumentSortField, Query()] = DocumentSortField.CREATED_AT,
    sort_dir: Annotated[SortDirection, Query()] = SortDirection.DESC,
) -> DocumentFilters:
    return DocumentFilters(
        status=status,
        document_type=document_type,
        search=search,
        owner_email=owner_email,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )


DocumentFilterParams = Annotated[DocumentFilters, Depends(get_document_filters)]


async def get_owned_document(
    document_id: Annotated[int, Path(ge=1, description="Document id.")],
    db: DBSession,
    current_user: CurrentUser,
) -> Document:
    """Load a document the caller is allowed to see, or 404.

    Ownership is enforced *in the query* (or by an explicit admin bypass), so no
    endpoint can forget the check. A document belonging to someone else returns
    404 rather than 403: a 403 would confirm that the id exists.
    """
    document = await doc_repo.get(
        db,
        document_id,
        owner_id=None if current_user.is_admin else current_user.id,
        with_details=True,
    )
    if document is None:
        raise NotFoundError(f"Document {document_id} was not found.")
    return document


OwnedDocument = Annotated[Document, Depends(get_owned_document)]


def get_client_info(request: Request) -> tuple[str | None, str | None]:
    """Extract ``(user_agent, client_ip)`` for the session audit trail.

    ``X-Forwarded-For`` is honoured because the app normally runs behind a proxy,
    where ``request.client.host`` is the load balancer. Only the leftmost entry is
    used, and it is treated as a hint for display - never for authorisation, since
    a client can forge the header unless the proxy overwrites it.
    """
    user_agent = request.headers.get("user-agent")

    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        client_ip: str | None = forwarded.split(",")[0].strip()
    else:
        client_ip = request.client.host if request.client else None

    return user_agent, client_ip


ClientInfo = Annotated[tuple[str | None, str | None], Depends(get_client_info)]
