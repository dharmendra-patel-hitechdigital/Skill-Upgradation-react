"""Read and write the runtime settings an administrator controls.

Deliberately not cached in process. The obvious optimisation - memoise the
active provider at startup - is wrong here: the service runs multiple replicas,
so a change made through one replica's admin panel would leave every other
replica analysing documents with the previous engine until it happened to
restart. That is a bug an operator cannot see and cannot explain.

The read instead happens once per document, on the session the pipeline already
opens to claim it (see ``document_processor._claim``). One indexed primary-key
lookup per document, against a pipeline that then spends seconds in an LLM call,
is not a cost worth caching away.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.app_setting import AppSetting
from app.models.user import User
from app.services.ai.registry import ANALYSIS_POLICIES

logger = logging.getLogger(__name__)

# The stored key. A constant because it appears in the endpoint, the pipeline and
# the tests, and a typo in any one of them would silently read a missing row as
# "no override configured".
ANALYSIS_PROVIDER_KEY = "analysis_provider"


async def get_raw(db: AsyncSession, key: str) -> str | None:
    """The stored value for ``key``, or None when unset."""
    return await db.scalar(select(AppSetting.value).where(AppSetting.key == key))


async def get_analysis_provider(db: AsyncSession) -> str | None:
    """The administrator's chosen analysis engine, or None to use the default.

    An unrecognised stored value is treated as unset. That keeps a stale row -
    a provider removed in a later build, say - from failing every upload; the
    registry logs the ignored value.
    """
    value = await get_raw(db, ANALYSIS_PROVIDER_KEY)
    if value is None:
        return None
    if value not in ANALYSIS_POLICIES:
        logger.warning("stored_analysis_provider_unknown", extra={"value": value})
        return None
    return value


async def set_analysis_provider(
    db: AsyncSession, value: str | None, *, changed_by: User
) -> str | None:
    """Upsert the chosen engine. ``None`` clears the override.

    Does not commit - the caller owns the transaction, per the repository
    convention.
    """
    if value is not None and value not in ANALYSIS_POLICIES:
        raise ValueError(f"Unknown analysis provider: {value!r}")

    record = await db.get(AppSetting, ANALYSIS_PROVIDER_KEY)
    if record is None:
        record = AppSetting(key=ANALYSIS_PROVIDER_KEY)
        db.add(record)

    record.value = value
    record.updated_by_id = changed_by.id
    await db.flush()

    logger.info(
        "analysis_provider_changed",
        extra={"value": value or "default", "changed_by": changed_by.id},
    )
    return value


async def describe_analysis_setting(db: AsyncSession) -> tuple[str | None, User | None]:
    """Return ``(stored_value, last_changed_by)`` for the admin panel."""
    record = await db.get(AppSetting, ANALYSIS_PROVIDER_KEY)
    if record is None:
        return None, None

    value = record.value if record.value in ANALYSIS_POLICIES else None
    user = (
        await db.get(User, record.updated_by_id)
        if record.updated_by_id is not None
        else None
    )
    return value, user
