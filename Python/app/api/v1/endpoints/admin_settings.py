"""Admin-only runtime settings: which AI engine analyses documents.

API keys are *not* managed here. They arrive as deployment secrets - an env var
locally, AWS Secrets Manager in a deployed environment - because a key that can
be written through an HTTP API is a key that can be exfiltrated through one. What
this endpoint controls is which of the *already configured* engines to use, which
is an operational decision worth making from the admin panel while watching the
analytics screen, without a redeploy.

The consequence is the rule enforced below: an engine can only be selected if its
credentials are already present. Selecting Claude on an installation with no
``ANTHROPIC_API_KEY`` would otherwise fail every upload from that moment on, with
the cause sitting in a settings screen nobody thinks to re-check.
"""
from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import AdminUser, DBSession
from app.core.config import settings
from app.core.exceptions import AIProviderUnavailableError, ValidationError
from app.models.app_setting import AppSetting
from app.schemas.ai_settings import AISettingsRead, AISettingsUpdate, AnalysisOptionRead
from app.schemas.common import ErrorResponse
from app.services import settings_service
from app.services.ai.registry import (
    analysis_options,
    effective_analysis_policy,
    get_analyzer,
)

router = APIRouter(
    prefix="/settings",
    tags=["Settings"],
    responses={
        401: {"model": ErrorResponse, "description": "Not authenticated"},
        403: {"model": ErrorResponse, "description": "Requires an administrator"},
    },
)


def _effective_engine(policy: str | None) -> str:
    """The engine that would actually run, with ``auto`` resolved to a concrete one.

    Reporting the resolved name matters: "auto" tells an administrator nothing
    about whether their new key was picked up, which is the one thing they opened
    this screen to check.
    """
    try:
        return get_analyzer(policy).name
    except AIProviderUnavailableError:
        return "disabled"


async def _build_response(db: DBSession) -> AISettingsRead:
    selected, changed_by = await settings_service.describe_analysis_setting(db)
    record = await db.get(AppSetting, settings_service.ANALYSIS_PROVIDER_KEY)
    record_at = record.updated_at if record else None

    return AISettingsRead(
        selected=selected,
        effective=_effective_engine(selected),
        default=settings.LLM_PROVIDER,
        is_override=selected is not None and selected != settings.LLM_PROVIDER,
        options=[
            AnalysisOptionRead(
                id=option.id,
                label=option.label,
                description=option.description,
                available=option.available,
                unavailable_reason=option.unavailable_reason,
                model=option.model,
            )
            for option in analysis_options()
        ],
        updated_at=record_at,
        updated_by=changed_by.email if changed_by else None,
    )


@router.get(
    "/ai",
    response_model=AISettingsRead,
    summary="Which AI engine analyses documents",
)
async def read_ai_settings(db: DBSession, _: AdminUser) -> AISettingsRead:
    """Report the selected engine, the engine that will actually run, and the options.

    `options[].available` reflects the configured credentials, so an engine whose
    key is missing is offered as disabled with the reason attached rather than
    hidden - "why is Claude not in the list" is a worse question than "Claude
    needs ANTHROPIC_API_KEY".
    """
    return await _build_response(db)


@router.put(
    "/ai",
    response_model=AISettingsRead,
    summary="Choose the AI engine for document analysis",
    responses={
        422: {"model": ErrorResponse, "description": "Unknown or unconfigured engine"}
    },
)
async def update_ai_settings(
    payload: AISettingsUpdate, db: DBSession, current_user: AdminUser
) -> AISettingsRead:
    """Set the analysis engine, or send `null` to return to the deployment default.

    Takes effect on the **next document processed** - a document already in the
    pipeline finishes on the engine it started with, rather than being switched
    mid-flight. Nothing is reprocessed automatically; use the reprocess action on
    a document to re-run it through the newly selected engine.
    """
    provider = payload.provider

    if provider is not None:
        option = next((o for o in analysis_options() if o.id == provider), None)
        if option is None:
            raise ValidationError(
                f"Unknown analysis engine '{provider}'.",
                details={"allowed": [o.id for o in analysis_options()]},
            )
        if not option.available:
            raise ValidationError(
                option.unavailable_reason
                or f"The {option.label} engine is not configured on this server.",
                details={"provider": provider},
            )

    await settings_service.set_analysis_provider(db, provider, changed_by=current_user)
    await db.commit()

    return await _build_response(db)


@router.get(
    "/ai/effective",
    summary="The engine that will run the next document",
    response_model=dict[str, str],
)
async def read_effective_engine(db: DBSession, _: AdminUser) -> dict[str, str]:
    """Just the resolved engine name - a cheap probe for a health check or a script."""
    selected = await settings_service.get_analysis_provider(db)
    return {
        "effective": _effective_engine(selected),
        "policy": effective_analysis_policy(selected),
    }
