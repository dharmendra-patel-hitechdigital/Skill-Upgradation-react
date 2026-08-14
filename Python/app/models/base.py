"""Shared column types and mixins for every ORM model.

The ``UtcDateTime`` type below exists to close a real bug class: SQLite (and
MySQL's ``DATETIME``) do not persist a timezone offset, so a value written as
timezone-aware comes back *naive*. Comparing that against
``datetime.now(UTC)`` raises ``TypeError``, and doing arithmetic on it silently
treats a UTC instant as local time. Normalising on the way in and re-attaching
UTC on the way out means the rest of the codebase only ever handles aware UTC
datetimes, on every supported database.
"""
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, Enum, TypeDecorator, func
from sqlalchemy.orm import Mapped, mapped_column


def portable_enum(enum_cls: type[StrEnum], *, length: int = 20) -> Enum:
    """A portable, value-based ``Enum`` column for a ``StrEnum``.

    Two non-obvious settings, both of which fix real bugs:

    ``values_callable``
        By default SQLAlchemy persists an enum's **member name** (``"ADMIN"``),
        not its value (``"admin"``). That silently disagrees with every other
        representation in the system - the JSON API, the ``server_default``, and
        anything a human types in a SQL console - so a row inserted by raw SQL or
        a data migration reads back as ``LookupError``. Storing values keeps the
        database, the API, and the defaults identical.

    ``native_enum=False``
        Emits a portable ``VARCHAR`` instead of a database-specific ``ENUM``
        type. Adding a member then costs no ``ALTER TYPE`` migration, and the
        same DDL works on SQLite, MySQL and Postgres.
    """
    return Enum(
        enum_cls,
        native_enum=False,
        length=length,
        validate_strings=True,
        values_callable=lambda enum: [member.value for member in enum],
    )


class UtcDateTime(TypeDecorator[datetime]):
    """Timezone-aware ``DATETIME`` that is stored as, and returned in, UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"Expected datetime, got {type(value).__name__}")
        if value.tzinfo is None:
            # A naive value is assumed to already be UTC - the only convention
            # this codebase writes.
            return value
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: Any, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def utcnow() -> datetime:
    """Single source of 'now' - patchable in tests, unambiguous in prod."""
    return datetime.now(UTC)


class TimestampMixin:
    """Adds audit timestamps maintained by the ORM.

    Python-side defaults are used (rather than relying solely on the database
    clock) so the value is identical across SQLite, MySQL and Postgres and does
    not depend on the server's session timezone. ``server_default`` is kept as
    a safety net for rows inserted by raw SQL or migrations.
    """

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        default=utcnow,
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
        nullable=False,
    )
