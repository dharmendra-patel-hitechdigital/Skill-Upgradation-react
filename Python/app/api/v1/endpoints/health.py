"""Health, readiness, and AI-provider introspection endpoints.

Liveness and readiness are separate on purpose, because orchestrators use them
for opposite decisions:

* ``/health/live`` - "is the process alive?" A failure here means **restart me**,
  so it must never touch a dependency. A database blip that fails liveness would
  trigger a restart loop that fixes nothing.
* ``/health/ready`` - "should traffic be routed here?" This *does* check the
  database, so a pod with a broken connection is pulled from the load balancer
  while staying up long enough to recover and be debugged.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status

from app.api.deps import DBSession
from app.core.config import settings
from app.core.database import check_database_connection
from app.services import settings_service
from app.services.ai.registry import describe_providers
from app.services.task_runner import task_runner

router = APIRouter(prefix="/health", tags=["Health"])


@router.get("/live", summary="Liveness probe")
async def liveness() -> dict[str, str]:
    """Return 200 whenever the process can serve requests. Checks no dependency."""
    return {"status": "ok", "service": settings.PROJECT_NAME, "version": settings.VERSION}


@router.get(
    "/ready",
    summary="Readiness probe",
    responses={503: {"description": "A required dependency is unavailable"}},
)
async def readiness(response: Response) -> dict[str, Any]:
    """Verify dependencies and report per-check detail.

    Returns **503** when a required dependency is down, so a load balancer stops
    sending traffic here.
    """
    database_ok = await check_database_connection()

    checks: dict[str, Any] = {
        "database": "ok" if database_ok else "unavailable",
        "background_tasks_in_flight": task_runner.in_flight,
    }

    if not database_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "degraded", "checks": checks}

    return {"status": "ok", "checks": checks}


@router.get("/providers", summary="Which AI providers are active")
async def providers(db: DBSession) -> dict[str, Any]:
    """Report the effective AI configuration.

    Answers "why did my scan fail?" and "which engine is analysing my documents?"
    without needing shell access to the server. It makes no upstream calls, so it
    is free to poll.

    The reported `analysis` engine accounts for the administrator's runtime
    choice, not just the deployment default - otherwise this endpoint would
    confidently name the wrong engine the moment anyone used the settings screen.
    """
    override = await settings_service.get_analysis_provider(db)
    status_report = describe_providers(override)
    return {
        "text_extraction": status_report.text_extraction,
        "analysis": status_report.analysis,
        "textract_available": status_report.textract_available,
        "openai_available": status_report.openai_available,
        "anthropic_available": status_report.anthropic_available,
        # The model belonging to the engine that is actually selected, not
        # whichever key happens to be present.
        "analysis_model": _analysis_model(status_report.analysis),
        "storage_backend": settings.STORAGE_BACKEND,
        "max_upload_mb": settings.MAX_UPLOAD_SIZE_MB,
        "accepted_types": settings.ALLOWED_UPLOAD_TYPES,
        "notes": status_report.notes,
    }


def _analysis_model(engine: str) -> str | None:
    """The model name for the engine that is actually running."""
    return {
        "claude": settings.ANTHROPIC_MODEL,
        "openai": settings.OPENAI_MODEL,
        "heuristic": "rules-v1",
    }.get(engine)
