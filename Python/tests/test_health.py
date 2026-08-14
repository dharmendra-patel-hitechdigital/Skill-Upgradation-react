"""Health, readiness, provider introspection, and the error envelope."""
from __future__ import annotations

from httpx import AsyncClient


async def test_root_points_at_the_docs(client: AsyncClient) -> None:
    response = await client.get("/")
    assert response.status_code == 200
    body = response.json()
    assert body["docs"] == "/docs"
    assert body["environment"] == "test"


async def test_liveness_touches_no_dependency(client: AsyncClient, api: str) -> None:
    response = await client.get(f"{api}/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_readiness_reports_the_database(client: AsyncClient, api: str) -> None:
    response = await client.get(f"{api}/health/ready")
    assert response.status_code == 200
    assert response.json()["checks"]["database"] == "ok"


async def test_providers_reports_the_offline_engines(client: AsyncClient, api: str) -> None:
    """With no credentials configured, both fallbacks must be reported honestly."""
    response = await client.get(f"{api}/health/providers")
    assert response.status_code == 200

    body = response.json()
    assert body["text_extraction"] == "local"
    assert body["analysis"] == "heuristic"
    assert body["openai_available"] is False
    assert body["textract_available"] is False
    # The operator must be told *why* capability is reduced, not left guessing.
    assert any("OpenAI" in note for note in body["notes"])


async def test_every_response_carries_a_request_id(client: AsyncClient, api: str) -> None:
    response = await client.get(f"{api}/health/live")
    assert response.headers["X-Request-ID"]
    assert float(response.headers["X-Process-Time-Ms"]) >= 0


async def test_inbound_request_id_is_preserved(client: AsyncClient, api: str) -> None:
    """A caller-supplied correlation id must survive, so traces join up."""
    response = await client.get(
        f"{api}/health/live", headers={"X-Request-ID": "trace-me-123"}
    )
    assert response.headers["X-Request-ID"] == "trace-me-123"


async def test_the_access_log_line_carries_the_request_id(
    client: AsyncClient, api: str, caplog
) -> None:
    """Regression guard: the contextvar must still be set when the access line
    is emitted. Resetting it first silently produced "-" for every request and
    made the log impossible to correlate with a reported request id."""
    from app.core.logging import RequestIdFilter

    caplog.handler.addFilter(RequestIdFilter())
    with caplog.at_level("INFO", logger="app.access"):
        response = await client.get(f"{api}/health/live")

    completed = [r for r in caplog.records if r.message == "request_completed"]
    assert completed, "no access log line was emitted"
    assert completed[0].request_id == response.headers["X-Request-ID"]
    assert completed[0].request_id != "-"


async def test_security_headers_are_present(client: AsyncClient) -> None:
    response = await client.get("/")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"


async def test_unknown_route_uses_the_standard_error_envelope(client: AsyncClient) -> None:
    response = await client.get("/api/v1/does-not-exist")
    assert response.status_code == 404

    error = response.json()["error"]
    assert error["code"] == "not_found"
    # The request id in the body must match the header so a user can quote either.
    assert error["request_id"] == response.headers["X-Request-ID"]


async def test_validation_errors_list_the_offending_fields(
    client: AsyncClient, api: str
) -> None:
    response = await client.post(
        f"{api}/auth/register", json={"email": "not-an-email", "password": "short"}
    )
    assert response.status_code == 422

    error = response.json()["error"]
    assert error["code"] == "validation_error"
    fields = {item["field"] for item in error["details"]["fields"]}
    assert fields == {"email", "password"}


async def test_openapi_schema_is_complete(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")
    assert response.status_code == 200

    schema = response.json()
    assert schema["info"]["title"]
    assert "/api/v1/documents" in schema["paths"]
    assert "servers" in schema
    assert schema["components"]["securitySchemes"]["HTTPBearer"]["bearerFormat"] == "JWT"


async def test_swagger_ui_renders(client: AsyncClient) -> None:
    response = await client.get("/docs")
    assert response.status_code == 200
    assert "swagger-ui" in response.text.lower()
