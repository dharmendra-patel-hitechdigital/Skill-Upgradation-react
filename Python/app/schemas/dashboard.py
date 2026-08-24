"""Dashboard response schemas.

These mirror the shapes the SPA already renders (``{stats: [...]}``,
``{series: [...]}``, ``{activity: [...]}``) so the dashboard needs no rewrite to
move off its built-in mock backend. Every field the client did not previously
have - ``unit``, ``comparison``, ``meta``, ``timestamp`` - is additive and
optional to consume, so an older bundle keeps working against a newer server.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class StatFormat(StrEnum):
    """How the client should render a stat's value."""

    NUMBER = "number"
    PERCENT = "percent"
    CURRENCY = "currency"


class Trend(StrEnum):
    """Direction of the delta.

    Only two members, because the UI has exactly two delta styles (green up,
    red down). A flat month is reported as ``up`` with ``delta: 0.0`` rather
    than inventing a third state the client cannot paint.
    """

    UP = "up"
    DOWN = "down"


class StatCard(BaseModel):
    """One headline metric."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "documents",
                "label": "Documents Processed",
                "value": 128,
                "format": "number",
                "delta": 12.5,
                "trend": "up",
                "unit": None,
                "comparison": "vs. previous 30 days",
            }
        }
    )

    id: str = Field(description="Stable key - safe to use as a React list key.")
    label: str
    value: int | float = Field(
        description="Raw number. Formatting is the client's job, per `format`."
    )
    format: StatFormat
    delta: float = Field(
        description=(
            "Change against the previous window: percent change for counts, "
            "percentage *points* for a `percent` stat."
        )
    )
    trend: Trend
    unit: str | None = Field(
        default=None, description="Suffix to display after the value, e.g. `MB`."
    )
    comparison: str = Field(description="Human label for what the delta compares.")


class StatsResponse(BaseModel):
    stats: list[StatCard]
    window_days: int = Field(description="Length of the window each stat covers.")
    generated_at: datetime


class SeriesPoint(BaseModel):
    """One bar in the volume chart."""

    label: str = Field(description="Month abbreviation, e.g. `Aug`. Unique in a series.")
    value: int


class SeriesMeta(BaseModel):
    """Labelling for the chart, so the axis text is not hard-coded client-side.

    The panel used to be captioned "Revenue, in thousands" against mock data.
    This service processes documents and holds no financial records, so the real
    series is upload volume - and the caption travels with it rather than being
    a string in the SPA that quietly contradicts the numbers.
    """

    title: str = Field(examples=["Document Volume"])
    subtitle: str = Field(examples=["Documents uploaded per month"])
    unit: str = Field(examples=["documents"])
    total: int = Field(description="Sum across the returned series.")
    year: int = Field(description="Calendar year of the most recent point.")


class SeriesResponse(BaseModel):
    """Time series for the chart panel.

    Served from ``/dashboard/revenue``: the path is kept for compatibility with
    the deployed bundle, but see :class:`SeriesMeta` for what the numbers are.
    """

    series: list[SeriesPoint]
    meta: SeriesMeta


class ActivityItem(BaseModel):
    """One row in the recent-activity feed."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": 412,
                "user": "Dharmendra Patel",
                "action": "completed extraction on invoice-q3.pdf",
                "amount": "248 KB",
                "time": "18 min ago",
                "type": "completed",
                "document_id": 87,
                "filename": "invoice-q3.pdf",
                "document_type": "invoice",
                "timestamp": "2026-08-24T09:41:12Z",
            }
        }
    )

    id: int = Field(description="Audit event id - unique, stable list key.")
    user: str = Field(description="Owner's display name, falling back to their email.")
    action: str
    amount: str = Field(
        default="", description="Right-aligned badge text; empty when there is none."
    )
    time: str = Field(description="Pre-formatted relative time, e.g. `2 min ago`.")
    type: str = Field(
        description="Icon/tone key: upload, processing, completed, failed, reprocess."
    )
    document_id: int
    filename: str
    document_type: str | None = None
    timestamp: datetime = Field(
        description="Exact instant, for clients that would rather format it themselves."
    )


class ActivityResponse(BaseModel):
    activity: list[ActivityItem]
