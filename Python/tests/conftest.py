"""Shared pytest fixtures.

Environment variables are set **before** any ``app`` import, because
``app.core.config.settings`` is instantiated at module import time and the
database engine is built from it. Configuring the process here - rather than
monkey-patching afterwards - means the tests exercise the same wiring production
uses, just pointed at a throwaway SQLite file and a temporary storage directory.

Consequences worth knowing:

* No ``dependency_overrides`` for the database. Requests *and* background
  document processing share one real engine, so the tests cover the actual
  cross-session behaviour of the pipeline instead of a mocked stand-in.
* ``LLM_PROVIDER=heuristic`` and ``OCR_PROVIDER=local``, so the full AI pipeline
  runs with no network access, no credentials, and no spend.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import AsyncGenerator
from pathlib import Path

TEST_ROOT = Path(tempfile.mkdtemp(prefix="ids-tests-"))

os.environ.update(
    {
        "ENVIRONMENT": "test",
        "DEBUG": "false",
        "DATABASE_URL": f"sqlite:///{(TEST_ROOT / 'test.db').as_posix()}",
        "SECRET_KEY": "test-only-secret-key-not-used-anywhere-real-0123456789",
        "ACCESS_TOKEN_EXPIRE_MINUTES": "30",
        "REFRESH_TOKEN_EXPIRE_DAYS": "7",
        "PASSWORD_MIN_LENGTH": "10",
        "STORAGE_BACKEND": "local",
        "STORAGE_LOCAL_DIR": str(TEST_ROOT / "documents"),
        # Force the offline engines: deterministic, free, and no network.
        "OCR_PROVIDER": "local",
        "LLM_PROVIDER": "heuristic",
        "OPENAI_API_KEY": "",
        "TEXTRACT_ENABLED": "false",
        "PROCESSING_MAX_CONCURRENCY": "4",
        "PROCESSING_TIMEOUT_SECONDS": "30",
        "MAX_UPLOAD_SIZE_MB": "5",
        "LOG_LEVEL": "WARNING",
        "LOG_JSON": "false",
        # Must be empty or startup would create an extra user and break the
        # "first registered account becomes admin" assertions.
        "FIRST_ADMIN_EMAIL": "",
        "FIRST_ADMIN_PASSWORD": "",
    }
)

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.database import Base, SessionFactory, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.services.storage import reset_storage_cache  # noqa: E402
from app.services.task_runner import task_runner  # noqa: E402
from tests.pdf_builder import build_pdf, contract_pdf, invoice_pdf  # noqa: E402

USER_EMAIL = "user@example.com"
USER_PASSWORD = "Sup3rSecretPass"
OTHER_EMAIL = "other@example.com"
OTHER_PASSWORD = "An0therSecretPass"


def pytest_sessionfinish(session, exitstatus) -> None:  # type: ignore[no-untyped-def]
    """Remove the temporary root once the whole run is over."""
    shutil.rmtree(TEST_ROOT, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def _create_schema() -> None:
    """Create the schema once for the whole run.

    Done through the **sync** driver deliberately: DDL needs no event loop, which
    avoids tying a session-scoped fixture to a particular asyncio loop, and it
    exercises ``settings.sync_database_url`` - the URL Alembic will use.
    """
    from sqlalchemy import create_engine

    settings.STORAGE_LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    sync_engine = create_engine(settings.sync_database_url)
    try:
        Base.metadata.create_all(sync_engine)
    finally:
        sync_engine.dispose()


@pytest.fixture(autouse=True)
async def _clean_tables() -> AsyncGenerator[None, None]:
    """Empty every table between tests so each starts from a known state.

    DELETE (rather than drop/create per test) keeps the suite fast, and reversed
    metadata order respects foreign keys without needing to disable constraints.
    """
    yield
    # Drain before truncating. A background pipeline still mid-write would
    # otherwise contend with the DELETEs (on SQLite, "database is locked") and
    # leak state into the next test.
    await task_runner.drain(timeout=30.0)
    task_runner.reopen()
    async with engine.begin() as connection:
        for table in reversed(Base.metadata.sorted_tables):
            await connection.execute(table.delete())
    # No sqlite_sequence reset is needed: these tables use plain INTEGER PRIMARY
    # KEY (rowid), not AUTOINCREMENT, so ids restart at 1 once the table is empty.


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """HTTP client wired straight to the ASGI app (no socket, no server)."""
    reset_storage_cache()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as async_client:
        yield async_client


@pytest.fixture
def api() -> str:
    return settings.API_V1_PREFIX


# ------------------------------------------------------------------- helpers
async def register(
    client: AsyncClient, email: str = USER_EMAIL, password: str = USER_PASSWORD, **extra
) -> dict:
    response = await client.post(
        f"{settings.API_V1_PREFIX}/auth/register",
        json={"email": email, "password": password, **extra},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def login(
    client: AsyncClient, email: str = USER_EMAIL, password: str = USER_PASSWORD
) -> dict:
    response = await client.post(
        f"{settings.API_V1_PREFIX}/auth/login",
        data={"username": email, "password": password},
    )
    assert response.status_code == 200, response.text
    return response.json()


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def user_tokens(client: AsyncClient) -> dict:
    """A registered, logged-in regular user.

    Registered second so the *first* account takes the auto-admin role and this
    one is a plain user - matching how the bootstrap rule actually behaves.
    """
    await register(client, "admin@example.com", "AdminPassw0rd")
    await register(client, USER_EMAIL, USER_PASSWORD, full_name="Test User")
    return await login(client, USER_EMAIL, USER_PASSWORD)


@pytest.fixture
async def admin_tokens(client: AsyncClient) -> dict:
    """The auto-promoted first account (admin), plus a second regular user."""
    await register(client, "admin@example.com", "AdminPassw0rd")
    await register(client, USER_EMAIL, USER_PASSWORD)
    return await login(client, "admin@example.com", "AdminPassw0rd")


@pytest.fixture
async def db_session() -> AsyncGenerator:
    """A raw session for tests that assert on database state directly."""
    async with SessionFactory() as session:
        yield session


async def drain_processing() -> None:
    """Wait for every queued document pipeline to finish.

    Uploads return 202 and process in the background. Rather than sleeping and
    hoping, tests await the runner directly - deterministic and as fast as the
    work actually takes.
    """
    await task_runner.drain(timeout=30.0)
    task_runner.reopen()


async def upload(
    client: AsyncClient,
    token: str,
    *,
    data: bytes | None = None,
    filename: str = "invoice.pdf",
    content_type: str = "application/pdf",
    process: bool = True,
) -> dict:
    """Upload a document and (by default) wait for processing to complete."""
    response = await client.post(
        f"{settings.API_V1_PREFIX}/documents",
        files={"file": (filename, data if data is not None else invoice_pdf(), content_type)},
        headers=auth_header(token),
    )
    assert response.status_code in (200, 202), response.text
    body = response.json()

    if process:
        await drain_processing()
        refreshed = await client.get(
            f"{settings.API_V1_PREFIX}/documents/{body['id']}",
            headers=auth_header(token),
        )
        assert refreshed.status_code == 200, refreshed.text
        return refreshed.json()
    return body


__all__ = [
    "OTHER_EMAIL",
    "OTHER_PASSWORD",
    "USER_EMAIL",
    "USER_PASSWORD",
    "auth_header",
    "build_pdf",
    "contract_pdf",
    "drain_processing",
    "invoice_pdf",
    "login",
    "register",
    "upload",
]
