"""The admin AI-engine picker: authorisation, validation, and effect on the pipeline."""
from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.services.ai import registry
from tests.conftest import auth_header, invoice_pdf, upload

PATH = "/settings/ai"


@pytest.fixture(autouse=True)
def _clear_provider_cache():
    """Analyzer factories are memoised, so a key flipped mid-test must invalidate them."""
    registry.reset_provider_cache()
    yield
    registry.reset_provider_cache()


# ----------------------------------------------------------------- authorisation
async def test_requires_authentication(client: AsyncClient, api: str) -> None:
    assert (await client.get(f"{api}{PATH}")).status_code == 401


async def test_a_regular_user_cannot_read_or_change_the_engine(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    headers = auth_header(user_tokens["access_token"])

    read = await client.get(f"{api}{PATH}", headers=headers)
    assert read.status_code == 403
    assert read.json()["error"]["code"] == "permission_denied"

    write = await client.put(f"{api}{PATH}", json={"provider": "heuristic"}, headers=headers)
    assert write.status_code == 403


# ------------------------------------------------------------------------- read
async def test_defaults_to_the_deployment_configuration(
    client: AsyncClient, api: str, admin_tokens: dict
) -> None:
    """With nothing stored, the deployment default applies and nothing is an override."""
    response = await client.get(
        f"{api}{PATH}", headers=auth_header(admin_tokens["access_token"])
    )
    assert response.status_code == 200

    body = response.json()
    assert body["selected"] is None
    assert body["is_override"] is False
    # The test environment pins LLM_PROVIDER=heuristic so the suite never needs a
    # key; a fresh install ships "auto".
    assert body["default"] == "heuristic"
    assert body["effective"] == "heuristic"
    assert body["updated_by"] is None


async def test_options_report_availability_and_the_reason(
    client: AsyncClient, api: str, admin_tokens: dict
) -> None:
    """A missing key must be explained, not hidden."""
    response = await client.get(
        f"{api}{PATH}", headers=auth_header(admin_tokens["access_token"])
    )
    options = {option["id"]: option for option in response.json()["options"]}

    assert set(options) == {"auto", "claude", "openai", "heuristic", "none"}
    # No keys are configured in the test environment.
    assert options["claude"]["available"] is False
    assert "ANTHROPIC_API_KEY" in options["claude"]["unavailable_reason"]
    assert options["openai"]["available"] is False
    assert "OPENAI_API_KEY" in options["openai"]["unavailable_reason"]
    # These two always work: no credentials, no network.
    assert options["heuristic"]["available"] is True
    assert options["none"]["available"] is True
    assert options["claude"]["model"] == "claude-opus-5"


# ------------------------------------------------------------------------ write
async def test_selecting_an_unconfigured_engine_is_rejected(
    client: AsyncClient, api: str, admin_tokens: dict
) -> None:
    """The rule that stops a settings change from failing every later upload."""
    response = await client.put(
        f"{api}{PATH}",
        json={"provider": "claude"},
        headers=auth_header(admin_tokens["access_token"]),
    )
    assert response.status_code == 422
    assert "ANTHROPIC_API_KEY" in response.json()["error"]["message"]


async def test_selecting_an_unknown_engine_is_rejected(
    client: AsyncClient, api: str, admin_tokens: dict
) -> None:
    response = await client.put(
        f"{api}{PATH}",
        json={"provider": "gemini"},
        headers=auth_header(admin_tokens["access_token"]),
    )
    assert response.status_code == 422
    assert "gemini" in response.json()["error"]["message"]


async def test_claude_becomes_selectable_once_the_key_is_configured(
    client: AsyncClient, api: str, admin_tokens: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of the feature: add the key, then pick the engine."""
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "ANTHROPIC_API_KEY", "sk-ant-test")
    registry.reset_provider_cache()
    headers = auth_header(admin_tokens["access_token"])

    listing = await client.get(f"{api}{PATH}", headers=headers)
    claude = next(o for o in listing.json()["options"] if o["id"] == "claude")
    assert claude["available"] is True
    assert claude["unavailable_reason"] is None
    # Configuring the key does not silently switch engines - the deployment
    # default still stands until someone chooses.
    assert listing.json()["effective"] == "heuristic"

    chosen = await client.put(f"{api}{PATH}", json={"provider": "claude"}, headers=headers)
    assert chosen.status_code == 200

    body = chosen.json()
    assert body["selected"] == "claude"
    assert body["effective"] == "claude"
    assert body["is_override"] is True
    assert body["updated_by"] == "admin@example.com"
    assert body["updated_at"] is not None


async def test_auto_prefers_claude_once_its_key_is_present(
    client: AsyncClient, api: str, admin_tokens: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under `auto`, a configured Claude key wins over the rule engine."""
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "LLM_PROVIDER", "auto")
    monkeypatch.setattr(app_settings, "ANTHROPIC_API_KEY", "sk-ant-test")
    registry.reset_provider_cache()

    response = await client.get(
        f"{api}{PATH}", headers=auth_header(admin_tokens["access_token"])
    )
    body = response.json()
    assert body["default"] == "auto"
    assert body["selected"] is None
    assert body["effective"] == "claude"


async def test_the_choice_persists_and_can_be_cleared(
    client: AsyncClient, api: str, admin_tokens: dict
) -> None:
    headers = auth_header(admin_tokens["access_token"])

    await client.put(f"{api}{PATH}", json={"provider": "heuristic"}, headers=headers)
    assert (await client.get(f"{api}{PATH}", headers=headers)).json()["selected"] == "heuristic"

    # null clears the override rather than deleting the audit row.
    cleared = await client.put(f"{api}{PATH}", json={"provider": None}, headers=headers)
    assert cleared.json()["selected"] is None
    assert cleared.json()["is_override"] is False
    # The audit trail survives the clear.
    assert cleared.json()["updated_by"] == "admin@example.com"


async def test_effective_probe_reports_the_resolved_engine(
    client: AsyncClient, api: str, admin_tokens: dict
) -> None:
    headers = auth_header(admin_tokens["access_token"])
    await client.put(f"{api}{PATH}", json={"provider": "none"}, headers=headers)

    probe = await client.get(f"{api}{PATH}/effective", headers=headers)
    assert probe.json() == {"effective": "disabled", "policy": "none"}


# --------------------------------------------------------------- pipeline effect
async def test_health_reports_the_selected_engine_not_just_the_default(
    client: AsyncClient, api: str, admin_tokens: dict
) -> None:
    """Otherwise this endpoint confidently names the wrong engine after any change."""
    headers = auth_header(admin_tokens["access_token"])
    await client.put(f"{api}{PATH}", json={"provider": "none"}, headers=headers)

    providers = await client.get(f"{api}/health/providers")
    assert providers.json()["analysis"] == "disabled"


async def test_the_selected_engine_is_used_for_the_next_document(
    client: AsyncClient, api: str, admin_tokens: dict
) -> None:
    """The override has to reach the background pipeline, not just the API."""
    headers = auth_header(admin_tokens["access_token"])
    await client.put(f"{api}{PATH}", json={"provider": "none"}, headers=headers)

    document = await upload(client, admin_tokens["access_token"], data=invoice_pdf())

    # Analysis is disabled, so the document must fail with that reason rather
    # than quietly completing on a different engine.
    assert document["status"] == "failed"
    assert "disabled" in document["error"]["message"].lower()


async def test_clearing_the_override_restores_processing(
    client: AsyncClient, api: str, admin_tokens: dict
) -> None:
    headers = auth_header(admin_tokens["access_token"])
    await client.put(f"{api}{PATH}", json={"provider": "none"}, headers=headers)
    await client.put(f"{api}{PATH}", json={"provider": None}, headers=headers)

    document = await upload(client, admin_tokens["access_token"], data=invoice_pdf())
    assert document["status"] == "completed"
    assert document["extraction"]["analysis_provider"] == "heuristic"
