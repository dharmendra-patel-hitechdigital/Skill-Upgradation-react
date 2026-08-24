"""Schemas for the admin AI-engine picker."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AnalysisOptionRead(BaseModel):
    """One selectable engine, with whether it can actually run."""

    id: str = Field(examples=["claude"])
    label: str = Field(examples=["Claude (Anthropic)"])
    description: str
    available: bool = Field(
        description="False when the engine's credentials are not configured."
    )
    unavailable_reason: str | None = Field(
        default=None,
        description="Why it cannot be selected - shown next to a disabled option.",
    )
    model: str | None = Field(
        default=None, description="The model this engine would use, when it has one."
    )


class AISettingsRead(BaseModel):
    """The current analysis-engine configuration."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "selected": "claude",
                "effective": "claude",
                "default": "auto",
                "is_override": True,
                "options": [],
                "updated_at": "2026-08-24T16:04:00Z",
                "updated_by": "admin@example.com",
            }
        }
    )

    selected: str | None = Field(
        description=(
            "The administrator's stored choice, or null when none is set and the "
            "deployment default applies."
        )
    )
    effective: str = Field(
        description=(
            "The engine that will actually run the next document - `selected` "
            "resolved against what is configured. `auto` resolves to a concrete "
            "engine here."
        )
    )
    default: str = Field(
        description="The deployment default (LLM_PROVIDER), used when nothing is selected."
    )
    is_override: bool = Field(
        description="True when a stored choice is overriding the deployment default."
    )
    options: list[AnalysisOptionRead]
    updated_at: datetime | None = None
    updated_by: str | None = Field(
        default=None, description="Email of the administrator who last changed this."
    )


class AISettingsUpdate(BaseModel):
    """Choose the analysis engine.

    ``null`` clears the override and returns to the deployment default, which is
    why the field is required-but-nullable rather than optional: an omitted field
    and an explicit null would otherwise be indistinguishable.
    """

    provider: str | None = Field(
        description=(
            "One of the ids from `options`, or null to clear the override. "
            "Selecting an engine whose credentials are missing is rejected."
        ),
        examples=["claude"],
    )
