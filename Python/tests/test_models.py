"""Model-level invariants: enum storage, UTC datetimes, derived properties."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects import mysql, sqlite

import app.models  # noqa: F401  (registers the tables)
from app.models.base import UtcDateTime, utcnow
from app.models.document import Document, DocumentStatus
from app.models.refresh_token import RefreshToken, RevocationReason
from app.models.user import User, UserRole


# --------------------------------------------------------------- enum storage
@pytest.mark.parametrize(
    ("model", "column", "enum_cls"),
    [
        (User, "role", UserRole),
        (Document, "status", DocumentStatus),
        (RefreshToken, "revoked_reason", RevocationReason),
    ],
)
def test_enums_are_stored_as_their_lowercase_values(
    model: type, column: str, enum_cls: type
) -> None:
    """SQLAlchemy stores enum *names* by default, which is the wrong choice here.

    Names ("ADMIN") disagree with the JSON API, the server defaults, and anything
    a human types in a SQL console. Storing values keeps all of them identical.
    """
    column_type = model.__table__.c[column].type
    bind = column_type.bind_processor(sqlite.dialect())

    for member in enum_cls:
        assert bind(member) == member.value
        assert member.value.islower() or "_" in member.value


@pytest.mark.parametrize(
    ("model", "column"),
    [(User, "role"), (Document, "status")],
)
def test_server_defaults_round_trip_through_the_enum(model: type, column: str) -> None:
    """A row inserted by raw SQL (relying on the default) must be readable.

    This is the regression guard for the bug where the ORM wrote "ADMIN" while the
    server default wrote "user", making such rows raise LookupError on read.
    """
    col = model.__table__.c[column]
    default_value = col.server_default.arg
    result = col.type.result_processor(sqlite.dialect(), None)

    assert result(str(default_value)) is not None


def test_enum_columns_are_portable_varchars_not_native_types() -> None:
    """A native ENUM would need an ALTER TYPE migration to add a member."""
    ddl = str(User.__table__.c.role.type.compile(dialect=mysql.dialect()))
    assert "VARCHAR" in ddl.upper()
    assert "ENUM(" not in ddl.upper()


# -------------------------------------------------------------- UTC datetimes
def test_naive_values_are_assumed_utc_and_returned_aware() -> None:
    column_type = UtcDateTime()
    bind = column_type.bind_processor(sqlite.dialect())
    result = column_type.result_processor(sqlite.dialect(), None)

    naive = datetime(2024, 3, 14, 12, 30, 0)
    assert bind(naive) == naive  # stored as-is
    restored = result(naive)
    assert restored.tzinfo is UTC  # comes back aware


def test_aware_values_are_converted_to_utc_before_storage() -> None:
    """Otherwise a +05:30 timestamp would be stored as if it were UTC."""
    column_type = UtcDateTime()
    bind = column_type.bind_processor(sqlite.dialect())

    ist = timezone(timedelta(hours=5, minutes=30))
    aware = datetime(2024, 3, 14, 18, 0, 0, tzinfo=ist)

    stored = bind(aware)
    assert stored == datetime(2024, 3, 14, 12, 30, 0)  # shifted to UTC
    assert stored.tzinfo is None  # and stripped, since DATETIME has no offset


def test_none_passes_through() -> None:
    column_type = UtcDateTime()
    assert column_type.bind_processor(sqlite.dialect())(None) is None
    assert column_type.result_processor(sqlite.dialect(), None)(None) is None


def test_a_non_datetime_is_rejected_loudly() -> None:
    with pytest.raises(TypeError):
        UtcDateTime().bind_processor(sqlite.dialect())("2024-03-14")


async def test_timestamps_survive_a_database_round_trip(db_session) -> None:
    """The whole point of UtcDateTime: comparisons against now() must not raise."""
    user = User(
        email="tz@example.com", hashed_password="x", role=UserRole.USER
    )
    db_session.add(user)
    await db_session.commit()

    loaded = await db_session.scalar(select(User).where(User.email == "tz@example.com"))
    assert loaded is not None
    assert loaded.created_at.tzinfo is not None
    # Naive/aware mixing would make this line a TypeError.
    assert (utcnow() - loaded.created_at).total_seconds() < 60


async def test_raw_sql_insert_relying_on_defaults_is_readable(db_session) -> None:
    """End-to-end proof of the enum/default fix, through a real database."""
    await db_session.execute(
        text(
            "INSERT INTO users (email, hashed_password) VALUES "
            "('rawsql@example.com', 'hash')"
        )
    )
    await db_session.commit()

    loaded = await db_session.scalar(
        select(User).where(User.email == "rawsql@example.com")
    )
    assert loaded is not None
    assert loaded.role is UserRole.USER  # the server default resolved cleanly
    assert loaded.is_active is True


# --------------------------------------------------------- derived properties
def test_processing_duration_needs_both_endpoints() -> None:
    document = Document()
    assert document.processing_duration_ms is None

    document.processing_started_at = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
    assert document.processing_duration_ms is None  # still running

    document.processing_finished_at = datetime(2024, 1, 1, 0, 0, 2, tzinfo=UTC)
    assert document.processing_duration_ms == 2000


def test_user_is_admin_helper() -> None:
    assert User(role=UserRole.ADMIN).is_admin
    assert not User(role=UserRole.USER).is_admin


def test_refresh_token_state_helpers() -> None:
    token = RefreshToken(jti="a", user_id=1, expires_at=utcnow() + timedelta(days=1))
    assert token.is_usable
    assert not token.is_revoked
    assert not token.is_expired
    assert not token.was_consumed_by_rotation

    token.revoked_at = utcnow()
    token.revoked_reason = RevocationReason.ROTATED
    assert token.is_revoked
    assert token.was_consumed_by_rotation
    assert not token.is_usable

    # Only rotation implies theft; logout is a mundane stale client.
    token.revoked_reason = RevocationReason.LOGOUT
    assert not token.was_consumed_by_rotation


def test_expired_token_is_not_usable() -> None:
    token = RefreshToken(jti="a", user_id=1, expires_at=utcnow() - timedelta(seconds=1))
    assert token.is_expired
    assert not token.is_usable


# ------------------------------------------------------------- lazy-load guard
async def test_relationships_refuse_implicit_lazy_loading(db_session) -> None:
    """`lazy="raise"` turns a hidden N+1 (or MissingGreenlet) into a loud failure."""
    from sqlalchemy.exc import InvalidRequestError

    user = User(email="lazy@example.com", hashed_password="x")
    db_session.add(user)
    await db_session.commit()

    loaded = await db_session.scalar(select(User).where(User.email == "lazy@example.com"))
    with pytest.raises(InvalidRequestError):
        _ = loaded.documents
