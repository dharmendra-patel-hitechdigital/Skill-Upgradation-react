"""Pipeline mechanics: the state machine, worker recovery, provider fallback,
concurrency limits, and the atomic claim that prevents double processing.
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.exceptions import AIProviderError, ConflictError
from app.models.document import DocumentStatus
from app.repositories import document as doc_repo
from app.services.document_processor import (
    assert_can_transition,
    can_transition,
    process_document,
    recover_stuck_documents,
)
from app.services.task_runner import BackgroundTaskRunner
from tests.conftest import auth_header, drain_processing, upload


# ------------------------------------------------------------- state machine
@pytest.mark.parametrize(
    ("current", "target"),
    [
        (DocumentStatus.PENDING, DocumentStatus.PROCESSING),
        (DocumentStatus.PROCESSING, DocumentStatus.COMPLETED),
        (DocumentStatus.PROCESSING, DocumentStatus.FAILED),
        (DocumentStatus.FAILED, DocumentStatus.PENDING),
        (DocumentStatus.COMPLETED, DocumentStatus.PENDING),
    ],
)
def test_legal_transitions(current: DocumentStatus, target: DocumentStatus) -> None:
    assert can_transition(current, target)
    assert_can_transition(current, target)  # must not raise


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (DocumentStatus.PENDING, DocumentStatus.COMPLETED),  # cannot skip processing
        (DocumentStatus.PENDING, DocumentStatus.FAILED),
        (DocumentStatus.COMPLETED, DocumentStatus.PROCESSING),  # must go via PENDING
        (DocumentStatus.FAILED, DocumentStatus.COMPLETED),
        (DocumentStatus.PROCESSING, DocumentStatus.PROCESSING),
    ],
)
def test_illegal_transitions_are_refused(
    current: DocumentStatus, target: DocumentStatus
) -> None:
    assert not can_transition(current, target)
    with pytest.raises(ConflictError, match="cannot move"):
        assert_can_transition(current, target)


def test_terminal_statuses() -> None:
    assert DocumentStatus.COMPLETED.is_terminal
    assert DocumentStatus.FAILED.is_terminal
    assert not DocumentStatus.PENDING.is_terminal
    assert not DocumentStatus.PROCESSING.is_terminal


# ------------------------------------------------------------------ atomic claim
async def _make_pending_document(db_session, *, owner_id: int = 1) -> int:
    """Insert a PENDING document directly, with no background task attached.

    Deliberately not via the HTTP endpoint: that would queue the real pipeline,
    which would claim the row before the test could, and the claim is precisely
    what is under test here.
    """
    document = await doc_repo.create(
        db_session,
        owner_id=owner_id,
        filename="direct.pdf",
        content_type="application/pdf",
        size_bytes=10,
        checksum_sha256="a" * 64,
        storage_key="unused/key",
        storage_backend="local",
    )
    await db_session.commit()
    return document.id


async def test_claim_is_atomic_so_a_document_is_processed_once(
    client, api: str, user_tokens: dict, db_session
) -> None:
    """The status check lives in the UPDATE's WHERE clause, so the DB picks a winner.

    Without this, two workers could both start the (billable) AI pipeline on the
    same upload.
    """
    owner_id = (
        await client.get(f"{api}/users/me", headers=auth_header(user_tokens["access_token"]))
    ).json()["id"]
    document_id = await _make_pending_document(db_session, owner_id=owner_id)

    first = await doc_repo.claim_for_processing(db_session, document_id)
    await db_session.commit()
    second = await doc_repo.claim_for_processing(db_session, document_id)
    await db_session.commit()

    assert first is True
    assert second is False  # already PROCESSING - the second worker backs off

    document = await doc_repo.get(db_session, document_id)
    assert document is not None
    assert document.status is DocumentStatus.PROCESSING
    assert document.attempt_count == 1  # incremented exactly once


async def test_processing_an_unclaimable_document_is_a_no_op(
    client, api: str, user_tokens: dict, db_session
) -> None:
    """A completed document must not be silently reprocessed by a stray task."""
    document = await upload(client, user_tokens["access_token"])
    assert document["status"] == "completed"

    await process_document(document["id"])  # must return without touching anything

    refreshed = await doc_repo.get(db_session, document["id"])
    assert refreshed is not None
    assert refreshed.status is DocumentStatus.COMPLETED
    assert refreshed.attempt_count == 1


async def test_processing_a_missing_document_does_not_raise() -> None:
    await process_document(999_999)


# --------------------------------------------------------------- crash recovery
async def test_stuck_documents_are_reclaimed_on_startup(
    client, api: str, user_tokens: dict, db_session
) -> None:
    """In-process work dies with the process; those rows must not poll forever."""
    from datetime import timedelta

    from app.models.base import utcnow

    pending = await upload(client, user_tokens["access_token"], process=False)
    await drain_processing()

    # Simulate a worker that was killed mid-pipeline, long enough ago to be stale.
    document = await doc_repo.get(db_session, pending["id"])
    assert document is not None
    document.status = DocumentStatus.PROCESSING
    document.processing_started_at = utcnow() - timedelta(hours=2)
    await db_session.commit()

    assert await recover_stuck_documents() == 1

    reclaimed = await doc_repo.get(db_session, pending["id"], with_details=True)
    await db_session.refresh(reclaimed)
    assert reclaimed.status is DocumentStatus.FAILED
    assert reclaimed.error_code == "worker_interrupted"
    # FAILED (not PENDING) so a restart loop cannot re-bill the AI provider.
    assert "restart" in (reclaimed.error_message or "").lower()

    # And it can be retried explicitly.
    retry = await client.post(
        f"{api}/documents/{pending['id']}/reprocess",
        headers=auth_header(user_tokens["access_token"]),
    )
    assert retry.status_code == 202
    await drain_processing()


async def test_recovery_leaves_fresh_processing_documents_alone(
    client, api: str, user_tokens: dict, db_session
) -> None:
    """A document that started one second ago is still running, not abandoned."""
    from app.models.base import utcnow

    pending = await upload(client, user_tokens["access_token"], process=False)
    await drain_processing()

    document = await doc_repo.get(db_session, pending["id"])
    document.status = DocumentStatus.PROCESSING
    document.processing_started_at = utcnow()
    await db_session.commit()

    assert await recover_stuck_documents() == 0


# ------------------------------------------------------------ analyser fallback
async def test_analysis_falls_back_when_the_primary_provider_fails(
    client, api: str, user_tokens: dict, monkeypatch
) -> None:
    """An OpenAI outage must degrade result quality, not lose the document."""
    from app.services.ai import registry
    from app.services.ai.heuristic import HeuristicAnalyzer

    class BrokenAnalyzer:
        name = "openai"
        model = "gpt-4o-mini"

        async def analyze(self, text, *, filename, content_type):
            raise AIProviderError("Simulated provider outage.")

        async def answer_question(self, text, question, *, filename):
            raise AIProviderError("Simulated provider outage.")

    monkeypatch.setattr(
        registry,
        "get_analyzer_with_fallback",
        lambda: (BrokenAnalyzer(), HeuristicAnalyzer()),
    )
    monkeypatch.setattr(
        "app.services.document_processor.get_analyzer_with_fallback",
        lambda: (BrokenAnalyzer(), HeuristicAnalyzer()),
    )

    document = await upload(client, user_tokens["access_token"])

    assert document["status"] == "completed"
    extraction = document["extraction"]
    assert extraction["analysis_provider"] == "heuristic"
    # The degradation is recorded, so nobody mistakes this for a model result.
    assert any(
        "unavailable" in warning.lower() for warning in extraction["warnings"]
    )


async def test_a_failing_provider_with_no_fallback_fails_the_document(
    client, api: str, user_tokens: dict, monkeypatch
) -> None:
    class BrokenAnalyzer:
        name = "openai"
        model = "gpt-4o-mini"

        async def analyze(self, text, *, filename, content_type):
            raise AIProviderError("Quota exhausted.")

        async def answer_question(self, text, question, *, filename):
            raise AIProviderError("Quota exhausted.")

    monkeypatch.setattr(
        "app.services.document_processor.get_analyzer_with_fallback",
        lambda: (BrokenAnalyzer(), None),
    )

    document = await upload(client, user_tokens["access_token"])
    assert document["status"] == "failed"
    assert document["error"]["code"] == "ai_provider_error"
    assert "Quota exhausted" in document["error"]["message"]


async def test_an_unexpected_crash_is_recorded_not_swallowed(
    client, api: str, user_tokens: dict, monkeypatch
) -> None:
    """A bug in the pipeline must still leave an explainable document, not a stuck row."""

    class ExplodingAnalyzer:
        name = "openai"
        model = "x"

        async def analyze(self, text, *, filename, content_type):
            raise RuntimeError("unexpected bug")

        async def answer_question(self, text, question, *, filename):
            raise RuntimeError("unexpected bug")

    monkeypatch.setattr(
        "app.services.document_processor.get_analyzer_with_fallback",
        lambda: (ExplodingAnalyzer(), None),
    )

    document = await upload(client, user_tokens["access_token"])
    assert document["status"] == "failed"
    assert document["error"]["code"] == "internal_error"
    # The opaque message must not leak internals, but must name the type.
    assert "RuntimeError" in document["error"]["message"]


# ------------------------------------------------------------------ task runner
async def test_runner_caps_concurrency() -> None:
    """Unbounded fan-out would open one AI call and one DB session per upload."""
    runner = BackgroundTaskRunner(max_concurrency=2)
    active = 0
    peak = 0
    release = asyncio.Event()

    async def job() -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await release.wait()
        active -= 1

    for index in range(6):
        runner.submit(job, name=f"job-{index}")

    await asyncio.sleep(0.05)  # let the first batch acquire the semaphore
    assert peak <= 2

    release.set()
    await runner.drain(timeout=5)
    assert peak == 2


async def test_runner_logs_but_does_not_propagate_task_failures(caplog) -> None:
    """A crashed task must be reported, not vanish into a GC-time warning."""
    runner = BackgroundTaskRunner(max_concurrency=1)

    async def boom() -> None:
        raise ValueError("kaboom")

    with caplog.at_level("ERROR"):
        runner.submit(boom, name="boom")
        await runner.drain(timeout=5)

    assert any("task_failed" in record.message for record in caplog.records)


async def test_runner_rejects_work_while_shutting_down() -> None:
    runner = BackgroundTaskRunner(max_concurrency=1)
    await runner.drain(timeout=1)

    async def job() -> None:  # pragma: no cover - must never run
        raise AssertionError("should not have been scheduled")

    assert runner.submit(job, name="late") is None

    runner.reopen()
    assert runner.submit(job, name="after-reopen") is not None
    await runner.drain(timeout=5)


async def test_drain_cancels_work_that_overruns_the_grace_period() -> None:
    runner = BackgroundTaskRunner(max_concurrency=1)
    started = asyncio.Event()

    async def slow() -> None:
        started.set()
        await asyncio.sleep(30)

    task = runner.submit(slow, name="slow")
    await started.wait()
    await runner.drain(timeout=0.1)

    assert task is not None
    assert task.cancelled() or task.done()
