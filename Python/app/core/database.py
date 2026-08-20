"""Async SQLAlchemy engine, session factory, and declarative base.

Why async? Every request in this service is I/O bound - a database round-trip,
an S3/Textract call, an OpenAI call. Async lets one worker process keep
thousands of those in flight instead of parking a thread per request.

Two session helpers are exposed on purpose:

* :func:`get_db` - the request-scoped FastAPI dependency. One session per
  request, rolled back and closed automatically.
* :func:`session_scope` - a standalone context manager for code that runs
  *outside* a request (background document processing, startup bootstrap, CLI).
  Background tasks must never reuse the request's session: the request may have
  already returned and closed it.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.core.config import settings

logger = logging.getLogger(__name__)


def _engine_kwargs() -> dict[str, Any]:
    """Pool configuration differs meaningfully between SQLite and real servers."""
    kwargs: dict[str, Any] = {
        "echo": settings.DB_ECHO,
        "future": True,
        # Emit a cheap SELECT 1 before handing out a pooled connection so a
        # connection dropped by the DB (or a load balancer) never surfaces as a
        # random 500 on the next request.
        "pool_pre_ping": True,
    }

    if settings.is_sqlite:
        # SQLite is a local file; connection pooling buys nothing and a shared
        # pool across the event loop causes "database is locked" surprises.
        kwargs["poolclass"] = NullPool
        kwargs["connect_args"] = {"check_same_thread": False}
        return kwargs

    kwargs.update(
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_recycle=settings.DB_POOL_RECYCLE_SECONDS,
    )

    # An explicit connection timeout matters for a managed database reached over
    # the internet: without it the driver's own (short) default can expire during
    # the TLS handshake and report a bare "Can't connect to MySQL server", which
    # looks like a firewall problem rather than a timeout. The keyword differs per
    # driver, so it is set per backend rather than blindly.
    timeout = settings.DB_CONNECT_TIMEOUT_SECONDS
    backend = make_url(settings.async_database_url).get_backend_name()
    if backend in ("mysql", "mariadb"):
        kwargs["connect_args"] = {"connect_timeout": timeout}
    elif backend == "postgresql":
        kwargs["connect_args"] = {"timeout": timeout}  # asyncpg spells it this way

    return kwargs


engine: AsyncEngine = create_async_engine(settings.async_database_url, **_engine_kwargs())

# expire_on_commit=False: after commit we still want to read attributes off the
# returned ORM object (to serialise a response) without triggering a lazy
# refresh - which in async-land would raise MissingGreenlet.
SessionFactory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model."""

    # Makes repr() on any model useful in logs and pdb without boilerplate.
    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} id={pk}>"


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a request-scoped session.

    The endpoint owns the commit. If it raises, we roll back so a half-applied
    unit of work is never left on the connection when it returns to the pool.
    """
    async with SessionFactory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """Transactional scope for non-request code (background jobs, startup).

    Commits on clean exit, rolls back on error.
    """
    async with SessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def check_database_connection() -> bool:
    """Lightweight liveness probe used by the readiness endpoint."""
    from sqlalchemy import text

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # pragma: no cover - depends on infra state
        logger.warning("database_unreachable", extra={"error": str(exc)})
        return False


async def dispose_engine() -> None:
    """Close every pooled connection - called on application shutdown."""
    await engine.dispose()
