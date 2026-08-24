"""Runtime configuration an administrator can change without a redeploy.

A deliberately tiny key/value table, not a growing column-per-knob one. Almost
everything this service needs is environment configuration - it belongs in the
deployment, not in a database an operator can edit at 2am. What lands here is the
narrow set of choices that must change *without* a redeploy and that are not
secrets.

The first such choice is which AI engine analyses documents: keys arrive as
deployment secrets (env vars, AWS Secrets Manager), but *which* configured engine
to use is an operational decision worth making from the admin panel while
watching the analytics screen.

Secrets never go in here. There is no encryption at rest on this column, values
are readable by anything with database access, and the value is echoed back over
the API to any administrator.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import UtcDateTime, utcnow


class AppSetting(Base):
    """One runtime setting. The key is the primary key - one row per setting."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)

    # Nullable so a setting can be explicitly cleared back to "use the
    # configured default" without deleting the row and losing the audit columns.
    value: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Who changed it last, for the audit trail. SET NULL rather than CASCADE: a
    # deleted administrator must not take the current configuration with them.
    updated_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
        nullable=False,
    )
