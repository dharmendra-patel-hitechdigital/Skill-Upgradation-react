"""End-to-end tests for the AI document-processing feature.

These run the *real* pipeline - upload, storage, PDF text extraction, analysis,
persistence - with the offline providers. No mocks, no network.
"""
from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import (
    USER_EMAIL,
    USER_PASSWORD,
    auth_header,
    build_pdf,
    contract_pdf,
    drain_processing,
    invoice_pdf,
    login,
    paused_processing,
    register,
    upload,
)


# ---------------------------------------------------------------------- upload
async def test_upload_returns_202_pending_before_processing(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    """The request must not block on OCR + analysis."""
    # Paused: every assertion below describes the state *before* the pipeline
    # runs, and the pipeline shares this event loop (see paused_processing).
    with paused_processing() as queued:
        response = await client.post(
            f"{api}/documents",
            files={"file": ("invoice.pdf", invoice_pdf(), "application/pdf")},
            headers=auth_header(user_tokens["access_token"]),
        )
    assert response.status_code == 202

    body = response.json()
    assert body["status"] == "pending"
    assert body["extraction"] is None
    assert body["filename"] == "invoice.pdf"
    assert body["size_bytes"] > 0
    assert len(body["checksum_sha256"]) == 64
    assert [event["event"] for event in body["events"]] == ["uploaded"]
    # 202 is only honest if the work was actually handed to the runner.
    assert queued == [f"process-document-{body['id']}"]


async def test_full_pipeline_extracts_and_analyses_an_invoice(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    document = await upload(client, user_tokens["access_token"])

    assert document["status"] == "completed"
    assert document["error"] is None
    assert document["page_count"] == 1
    assert document["processing_duration_ms"] is not None

    extraction = document["extraction"]
    assert extraction is not None

    # Stage 1: the PDF text layer was actually read.
    assert extraction["ocr_provider"] == "local"
    assert extraction["text_char_count"] > 300
    assert "ACME TECHNOLOGIES" in extraction["text_preview"]

    # Stage 2: the document was classified, summarised, and mined for fields.
    assert extraction["analysis_provider"] == "heuristic"
    assert document["document_type"] == "invoice"
    assert extraction["summary"]
    assert 0.0 < extraction["confidence"] <= 1.0
    assert extraction["keywords"]

    # The invoice's business-critical values must be present.
    fields = {field["key"]: field["value"] for field in extraction["fields"]}
    assert fields.get("invoice_number") == "INV-2024-00871"
    assert fields.get("due_date") == "13/04/2024"
    assert "1488.00" in (fields.get("total_amount") or "")

    entity_types = {entity["type"] for entity in extraction["entities"]}
    assert {"email", "money", "date"} <= entity_types
    emails = {e["text"] for e in extraction["entities"] if e["type"] == "email"}
    assert "accounts@northwind-trading.example" in emails

    # The audit trail records the whole lifecycle.
    assert [event["event"] for event in document["events"]] == [
        "uploaded",
        "processing_started",
        "processing_completed",
    ]


async def test_pipeline_handles_a_multi_page_contract(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    document = await upload(
        client, user_tokens["access_token"], data=contract_pdf(), filename="nda.pdf"
    )
    assert document["status"] == "completed"
    assert document["page_count"] == 2
    assert document["document_type"] == "contract"
    # Text from both pages must be present.
    assert document["extraction"]["text_char_count"] > 400


async def test_plain_text_upload_is_processed(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    body = b"Dear Ms Harper,\n\nEnclosed is the report you requested.\n\nSincerely,\nJ. Blackwood\n"
    document = await upload(
        client,
        user_tokens["access_token"],
        data=body,
        filename="letter.txt",
        content_type="text/plain",
    )
    assert document["status"] == "completed"
    assert document["document_type"] == "letter"


async def test_identical_reupload_is_deduplicated(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    """Re-uploading the same bytes must not pay for a second AI run."""
    token = user_tokens["access_token"]
    first = await upload(client, token)

    again = await client.post(
        f"{api}/documents",
        files={"file": ("invoice-copy.pdf", invoice_pdf(), "application/pdf")},
        headers=auth_header(token),
    )
    assert again.status_code == 200  # 200, not 202: nothing new was queued
    assert again.json()["id"] == first["id"]
    assert again.json()["attempt_count"] == 1

    listing = await client.get(f"{api}/documents", headers=auth_header(token))
    assert listing.json()["meta"]["total"] == 1


async def test_different_users_may_upload_the_same_file(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    """Deduplication is per user - it must not leak one user's document to another."""
    first = await upload(client, user_tokens["access_token"])

    await register(client, "third@example.com", "ThirdPassw0rd")
    other = await login(client, "third@example.com", "ThirdPassw0rd")
    second = await upload(client, other["access_token"])

    assert first["id"] != second["id"]


# ------------------------------------------------------------------ validation
async def test_upload_requires_authentication(client: AsyncClient, api: str) -> None:
    response = await client.post(
        f"{api}/documents",
        files={"file": ("invoice.pdf", invoice_pdf(), "application/pdf")},
    )
    assert response.status_code == 401


async def test_unsupported_type_is_rejected(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    response = await client.post(
        f"{api}/documents",
        files={"file": ("payload.zip", b"PK\x03\x04rest-of-a-zip", "application/zip")},
        headers=auth_header(user_tokens["access_token"]),
    )
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_media_type"


async def test_content_type_is_sniffed_not_trusted(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    """A file mislabelled as a PDF must be identified by its bytes and refused."""
    response = await client.post(
        f"{api}/documents",
        files={"file": ("evil.pdf", b"MZ\x90\x00\x03executable", "application/pdf")},
        headers=auth_header(user_tokens["access_token"]),
    )
    assert response.status_code == 415


async def test_a_real_pdf_mislabelled_as_octet_stream_is_accepted(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    """Sniffing also *rescues* correct files that browsers label badly."""
    response = await client.post(
        f"{api}/documents",
        files={"file": ("mystery.pdf", invoice_pdf(), "application/octet-stream")},
        headers=auth_header(user_tokens["access_token"]),
    )
    assert response.status_code == 202
    assert response.json()["content_type"] == "application/pdf"
    await drain_processing()


async def test_binary_disguised_as_text_is_rejected(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    response = await client.post(
        f"{api}/documents",
        files={"file": ("notes.txt", b"text\x00\x01\x02binary", "text/plain")},
        headers=auth_header(user_tokens["access_token"]),
    )
    assert response.status_code == 415


async def test_empty_file_is_rejected(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    response = await client.post(
        f"{api}/documents",
        files={"file": ("empty.pdf", b"", "application/pdf")},
        headers=auth_header(user_tokens["access_token"]),
    )
    assert response.status_code in (415, 422)


async def test_oversized_upload_is_rejected(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    """The limit is enforced on bytes received, not on a client's Content-Length."""
    oversized = b"%PDF-1.4\n" + b"A" * (6 * 1024 * 1024)  # limit is 5 MB in tests
    response = await client.post(
        f"{api}/documents",
        files={"file": ("huge.pdf", oversized, "application/pdf")},
        headers=auth_header(user_tokens["access_token"]),
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


async def test_pdf_without_a_text_layer_fails_with_an_actionable_message(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    """A scan needs OCR. The failure must say so instead of reporting empty success."""
    blank = build_pdf([[""]])
    document = await upload(
        client, user_tokens["access_token"], data=blank, filename="scan.pdf"
    )

    assert document["status"] == "failed"
    assert document["error"]["code"] == "extraction_failed"
    assert "textract" in document["error"]["message"].lower()
    assert document["extraction"] is None
    assert "processing_failed" in [event["event"] for event in document["events"]]


# ------------------------------------------------------------- retrieval & list
async def test_list_is_paginated_and_filterable(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    token = user_tokens["access_token"]
    await upload(client, token, data=invoice_pdf(), filename="invoice.pdf")
    await upload(client, token, data=contract_pdf(), filename="nda.pdf")

    headers = auth_header(token)

    listing = await client.get(f"{api}/documents", headers=headers)
    assert listing.json()["meta"]["total"] == 2

    by_status = await client.get(f"{api}/documents?status=completed", headers=headers)
    assert by_status.json()["meta"]["total"] == 2

    by_type = await client.get(f"{api}/documents?document_type=invoice", headers=headers)
    assert by_type.json()["meta"]["total"] == 1

    by_search = await client.get(f"{api}/documents?search=NDA", headers=headers)
    assert by_search.json()["meta"]["total"] == 1  # case-insensitive

    none_match = await client.get(f"{api}/documents?status=failed", headers=headers)
    assert none_match.json()["meta"]["total"] == 0


async def test_list_sorting(client: AsyncClient, api: str, user_tokens: dict) -> None:
    token = user_tokens["access_token"]
    await upload(client, token, data=invoice_pdf(), filename="alpha.pdf")
    await upload(client, token, data=contract_pdf(), filename="zulu.pdf")

    ascending = await client.get(
        f"{api}/documents?sort_by=filename&sort_dir=asc", headers=auth_header(token)
    )
    names = [item["filename"] for item in ascending.json()["items"]]
    assert names == ["alpha.pdf", "zulu.pdf"]


async def test_list_excludes_the_heavy_text_payload(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    """Listing must not drag every document's OCR text through the database."""
    await upload(client, user_tokens["access_token"])
    listing = await client.get(
        f"{api}/documents", headers=auth_header(user_tokens["access_token"])
    )
    item = listing.json()["items"][0]
    assert "extraction" not in item
    assert "text_preview" not in item


async def test_stats_counts_by_status(client: AsyncClient, api: str, user_tokens: dict) -> None:
    token = user_tokens["access_token"]
    await upload(client, token)
    await upload(client, token, data=build_pdf([[""]]), filename="scan.pdf")

    response = await client.get(f"{api}/documents/stats", headers=auth_header(token))
    assert response.status_code == 200

    stats = response.json()
    assert stats["completed"] == 1
    assert stats["failed"] == 1
    assert stats["pending"] == 0


async def test_full_text_endpoint(client: AsyncClient, api: str, user_tokens: dict) -> None:
    token = user_tokens["access_token"]
    document = await upload(client, token)

    response = await client.get(
        f"{api}/documents/{document['id']}/text", headers=auth_header(token)
    )
    assert response.status_code == 200

    body = response.json()
    assert "INV-2024-00871" in body["text"]
    assert body["char_count"] == len(body["text"])
    assert body["ocr_provider"] == "local"


async def test_text_endpoint_conflicts_before_processing(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    token = user_tokens["access_token"]
    with paused_processing():
        pending = await upload(client, token, process=False)
        response = await client.get(
            f"{api}/documents/{pending['id']}/text", headers=auth_header(token)
        )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "document_not_ready"


async def test_download_returns_the_original_bytes(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    token = user_tokens["access_token"]
    original = invoice_pdf()
    document = await upload(client, token, data=original)

    response = await client.get(
        f"{api}/documents/{document['id']}/download", headers=auth_header(token)
    )
    assert response.status_code == 200
    assert response.content == original
    assert response.headers["content-type"] == "application/pdf"
    assert "attachment" in response.headers["content-disposition"]


# ------------------------------------------------------------------- ownership
async def test_one_user_cannot_see_anothers_document(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    """Must be 404, not 403 - a 403 would confirm the id exists."""
    mine = await upload(client, user_tokens["access_token"])

    await register(client, "nosy@example.com", "NosyPassw0rd")
    intruder = await login(client, "nosy@example.com", "NosyPassw0rd")
    headers = auth_header(intruder["access_token"])

    for path in ("", "/text", "/download"):
        response = await client.get(f"{api}/documents/{mine['id']}{path}", headers=headers)
        assert response.status_code == 404, path

    assert (
        await client.delete(f"{api}/documents/{mine['id']}", headers=headers)
    ).status_code == 404
    assert (
        await client.post(
            f"{api}/documents/{mine['id']}/ask", json={"question": "What is this?"}, headers=headers
        )
    ).status_code == 404


async def test_admin_can_see_every_document(
    client: AsyncClient, api: str, admin_tokens: dict
) -> None:
    user = await login(client, USER_EMAIL, USER_PASSWORD)
    document = await upload(client, user["access_token"])

    response = await client.get(
        f"{api}/documents/{document['id']}", headers=auth_header(admin_tokens["access_token"])
    )
    assert response.status_code == 200

    listing = await client.get(
        f"{api}/documents", headers=auth_header(admin_tokens["access_token"])
    )
    assert listing.json()["meta"]["total"] == 1


async def test_missing_document_is_404(client: AsyncClient, api: str, user_tokens: dict) -> None:
    response = await client.get(
        f"{api}/documents/9999", headers=auth_header(user_tokens["access_token"])
    )
    assert response.status_code == 404


# --------------------------------------------------------------------- ask (QA)
async def test_ask_finds_an_answer_present_in_the_document(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    token = user_tokens["access_token"]
    document = await upload(client, token)

    response = await client.post(
        f"{api}/documents/{document['id']}/ask",
        json={"question": "What is the total amount due on this invoice?"},
        headers=auth_header(token),
    )
    assert response.status_code == 200

    body = response.json()
    assert body["answer_found"] is True
    assert body["provider"] == "heuristic"
    assert body["quotes"]
    # The answer must be grounded in the document, not invented.
    assert "1488.00" in " ".join(body["quotes"]) or "Amount Due" in " ".join(body["quotes"])


async def test_ask_admits_when_the_answer_is_absent(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    """The honest 'not found' path is the main defence against hallucination."""
    token = user_tokens["access_token"]
    document = await upload(client, token)

    response = await client.post(
        f"{api}/documents/{document['id']}/ask",
        json={"question": "What zebra migration statistics appear in Antarctica?"},
        headers=auth_header(token),
    )
    assert response.status_code == 200
    assert response.json()["answer_found"] is False


async def test_ask_requires_a_completed_document(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    token = user_tokens["access_token"]
    with paused_processing():
        pending = await upload(client, token, process=False)
        response = await client.post(
            f"{api}/documents/{pending['id']}/ask",
            json={"question": "What is the total?"},
            headers=auth_header(token),
        )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "document_not_ready"


async def test_ask_validates_the_question(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    token = user_tokens["access_token"]
    document = await upload(client, token)

    response = await client.post(
        f"{api}/documents/{document['id']}/ask",
        json={"question": "x"},
        headers=auth_header(token),
    )
    assert response.status_code == 422


# ------------------------------------------------------------------- reprocess
async def test_reprocess_reruns_the_pipeline(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    token = user_tokens["access_token"]
    document = await upload(client, token)
    assert document["attempt_count"] == 1

    response = await client.post(
        f"{api}/documents/{document['id']}/reprocess", headers=auth_header(token)
    )
    assert response.status_code == 202
    assert response.json()["status"] == "pending"

    await drain_processing()

    after = await client.get(f"{api}/documents/{document['id']}", headers=auth_header(token))
    body = after.json()
    assert body["status"] == "completed"
    assert body["attempt_count"] == 2
    # The extraction is replaced, not duplicated.
    assert body["extraction"] is not None
    assert "reprocess_requested" in [event["event"] for event in body["events"]]


async def test_reprocess_recovers_a_failed_document(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    token = user_tokens["access_token"]
    failed = await upload(client, token, data=build_pdf([[""]]), filename="scan.pdf")
    assert failed["status"] == "failed"

    response = await client.post(
        f"{api}/documents/{failed['id']}/reprocess", headers=auth_header(token)
    )
    assert response.status_code == 202
    await drain_processing()

    after = await client.get(f"{api}/documents/{failed['id']}", headers=auth_header(token))
    # Still unreadable, so it fails again - but the error is fresh, not stale.
    assert after.json()["status"] == "failed"
    assert after.json()["attempt_count"] == 2


async def test_reprocess_rejects_an_in_flight_document(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    """A second run would duplicate billable AI calls for no benefit."""
    token = user_tokens["access_token"]
    with paused_processing():
        pending = await upload(client, token, process=False)
        response = await client.post(
            f"{api}/documents/{pending['id']}/reprocess", headers=auth_header(token)
        )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "already_queued"


# ---------------------------------------------------------------------- delete
async def test_delete_removes_the_record_and_the_stored_file(
    client: AsyncClient, api: str, user_tokens: dict, db_session
) -> None:
    from sqlalchemy import func, select

    from app.models.document import DocumentEvent, DocumentExtraction
    from app.services.storage import get_storage

    token = user_tokens["access_token"]
    document = await upload(client, token)

    storage_key = (
        await db_session.execute(
            select(__import__("app.models.document", fromlist=["Document"]).Document.storage_key)
        )
    ).scalar_one()
    assert await get_storage().exists(storage_key)

    response = await client.delete(f"{api}/documents/{document['id']}", headers=auth_header(token))
    assert response.status_code == 200

    assert (
        await client.get(f"{api}/documents/{document['id']}", headers=auth_header(token))
    ).status_code == 404

    # The blob is gone, and the child rows cascaded.
    assert not await get_storage().exists(storage_key)
    assert (
        await db_session.scalar(select(func.count()).select_from(DocumentExtraction))
    ) == 0
    assert (await db_session.scalar(select(func.count()).select_from(DocumentEvent))) == 0
