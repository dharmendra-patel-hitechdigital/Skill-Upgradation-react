"""Document analytics: correctness of the aggregates, and per-role scoping."""
from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import (
    USER_EMAIL,
    USER_PASSWORD,
    auth_header,
    contract_pdf,
    invoice_pdf,
    login,
    upload,
)

PATH = "/analytics/documents"


async def test_requires_authentication(client: AsyncClient, api: str) -> None:
    response = await client.get(f"{api}{PATH}")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"


async def test_empty_account_reports_zeros_not_nulls(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    """The first-ever load is the empty case, so it is the one that must work.

    Every aggregate here is a SUM or AVG over no rows, which SQL answers with
    NULL - each one has to be coalesced before it reaches the client.
    """
    response = await client.get(
        f"{api}{PATH}", headers=auth_header(user_tokens["access_token"])
    )
    assert response.status_code == 200
    body = response.json()

    assert body["totals"] == {
        "documents": 0,
        "completed": 0,
        "failed": 0,
        "in_progress": 0,
        "success_rate": 0.0,
        "pages": 0,
        "size_bytes": 0,
        "reprocessed": 0,
    }
    assert body["by_type"] == []
    assert body["failures"] == []
    assert body["daily"] == []
    assert body["performance"]["samples"] == 0
    assert body["tokens"]["total_tokens"] == 0
    # The histogram keeps its buckets even when empty, so the chart has an axis.
    assert [bucket["documents"] for bucket in body["confidence"]] == [0, 0, 0, 0]


async def test_totals_and_type_mix_reflect_real_uploads(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    token = user_tokens["access_token"]
    await upload(client, token, data=invoice_pdf(), filename="invoice.pdf")
    await upload(client, token, data=contract_pdf(), filename="contract.pdf")

    body = (await client.get(f"{api}{PATH}", headers=auth_header(token))).json()

    assert body["scope"] == "own"
    assert body["totals"]["documents"] == 2
    assert body["totals"]["completed"] == 2
    assert body["totals"]["success_rate"] == 100.0
    assert body["totals"]["pages"] >= 2
    assert body["totals"]["reprocessed"] == 0

    types = {entry["document_type"]: entry for entry in body["by_type"]}
    assert set(types) == {"invoice", "contract"}
    for entry in types.values():
        assert entry["documents"] == 1
        assert entry["share"] == 50.0
        assert 0.0 <= entry["avg_confidence"] <= 1.0

    # Shares are a partition of the whole, so they must add up.
    assert round(sum(e["share"] for e in body["by_type"]), 1) == 100.0


async def test_performance_reports_the_pipeline_split(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    token = user_tokens["access_token"]
    await upload(client, token, data=invoice_pdf())

    performance = (
        await client.get(f"{api}{PATH}", headers=auth_header(token))
    ).json()["performance"]

    assert performance["samples"] == 1
    assert performance["avg_total_ms"] is not None
    # With one sample every percentile is that sample - and critically p95 must
    # not be null from an OFFSET that ran past the end of the result set.
    assert performance["p50_total_ms"] == performance["p95_total_ms"]
    assert performance["slowest_total_ms"] >= 0
    assert performance["avg_ms_per_page"] is not None


async def test_providers_report_the_offline_engines(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    """The fallbacks engage silently, so this panel is how anyone finds out."""
    token = user_tokens["access_token"]
    await upload(client, token, data=invoice_pdf())

    body = (await client.get(f"{api}{PATH}", headers=auth_header(token))).json()
    providers = {(p["stage"], p["provider"]): p for p in body["providers"]}

    assert ("text_extraction", "local") in providers
    assert ("analysis", "heuristic") in providers
    # Share is per stage, not of all extraction rows: one document through one
    # engine is 100% of that stage.
    assert providers[("analysis", "heuristic")]["share"] == 100.0

    # The heuristic analyser spends no tokens, and reporting zero is correct -
    # `documents_with_tokens` is what distinguishes that from missing data.
    assert body["tokens"]["total_tokens"] == 0
    assert body["tokens"]["documents_with_tokens"] == 0


async def test_failures_are_grouped_by_code(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    token = user_tokens["access_token"]
    # A PDF with no text layer fails at extraction, which is a real failure path
    # rather than an injected one.
    await upload(client, token, data=b"%PDF-1.4\n%%EOF\n", filename="blank.pdf")

    body = (await client.get(f"{api}{PATH}", headers=auth_header(token))).json()

    assert body["totals"]["failed"] == 1
    assert body["totals"]["success_rate"] == 0.0
    assert len(body["failures"]) == 1

    failure = body["failures"][0]
    assert failure["documents"] == 1
    # Share of failures, not of all documents.
    assert failure["share"] == 100.0
    assert failure["code"]
    assert failure["example_message"]


async def test_reprocessing_is_counted_as_repeated_work(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    from tests.conftest import drain_processing

    token = user_tokens["access_token"]
    document = await upload(client, token, data=invoice_pdf())

    reprocess = await client.post(
        f"{api}/documents/{document['id']}/reprocess", headers=auth_header(token)
    )
    assert reprocess.status_code == 202
    await drain_processing()

    body = (await client.get(f"{api}{PATH}", headers=auth_header(token))).json()
    assert body["totals"]["documents"] == 1
    assert body["totals"]["reprocessed"] == 1


async def test_daily_series_buckets_by_calendar_day(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    token = user_tokens["access_token"]
    await upload(client, token, data=invoice_pdf())
    await upload(client, token, data=contract_pdf())

    daily = (await client.get(f"{api}{PATH}", headers=auth_header(token))).json()["daily"]

    # Both uploads happen in this test, so they land in one bucket - today.
    assert len(daily) == 1
    assert daily[0]["documents"] == 2
    assert daily[0]["completed"] == 2
    assert daily[0]["failed"] == 0


async def test_a_user_sees_only_their_own_figures(
    client: AsyncClient, api: str, admin_tokens: dict
) -> None:
    """Scope comes from the caller's role, so it cannot be widened by request."""
    user = await login(client, USER_EMAIL, USER_PASSWORD)
    await upload(client, user["access_token"], data=invoice_pdf())

    admin_view = (
        await client.get(f"{api}{PATH}", headers=auth_header(admin_tokens["access_token"]))
    ).json()
    assert admin_view["scope"] == "installation"
    assert admin_view["totals"]["documents"] == 1

    # The admin's own account uploaded nothing.
    own_view = (
        await client.get(f"{api}{PATH}", headers=auth_header(user["access_token"]))
    ).json()
    assert own_view["scope"] == "own"
    assert own_view["totals"]["documents"] == 1
    assert own_view["top_uploaders"] == [], "a non-admin gets no uploader ranking"


async def test_admin_uploader_ranking_names_the_busiest(
    client: AsyncClient, api: str, admin_tokens: dict
) -> None:
    user = await login(client, USER_EMAIL, USER_PASSWORD)
    await upload(client, user["access_token"], data=invoice_pdf())
    await upload(client, user["access_token"], data=contract_pdf())
    await upload(client, admin_tokens["access_token"], data=invoice_pdf())

    body = (
        await client.get(f"{api}{PATH}", headers=auth_header(admin_tokens["access_token"]))
    ).json()

    ranking = body["top_uploaders"]
    assert [entry["documents"] for entry in ranking] == [2, 1], "busiest first"
    assert ranking[0]["email"] == USER_EMAIL
    assert body["totals"]["documents"] == 3


async def test_window_is_validated_and_reported(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    headers = auth_header(user_tokens["access_token"])

    ok = await client.get(f"{api}{PATH}?window_days=7", headers=headers)
    assert ok.json()["window_days"] == 7

    rejected = await client.get(f"{api}{PATH}?window_days=0", headers=headers)
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "validation_error"


async def test_documents_outside_the_window_are_excluded(
    client: AsyncClient, api: str, user_tokens: dict, db_session
) -> None:
    """A window of 1 day must not count an upload backdated beyond it."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from app.models.document import Document

    token = user_tokens["access_token"]
    document = await upload(client, token, data=invoice_pdf())

    await db_session.execute(
        update(Document)
        .where(Document.id == document["id"])
        .values(created_at=datetime.now(UTC) - timedelta(days=10))
    )
    await db_session.commit()

    body = (
        await client.get(f"{api}{PATH}?window_days=1", headers=auth_header(token))
    ).json()
    assert body["totals"]["documents"] == 0

    wider = (
        await client.get(f"{api}{PATH}?window_days=30", headers=auth_header(token))
    ).json()
    assert wider["totals"]["documents"] == 1
