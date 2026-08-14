"""In-process background task runner for the document pipeline.

Why not just ``asyncio.create_task``?
------------------------------------
A bare task has three problems this class fixes:

1. **It can be garbage collected mid-flight.** The event loop keeps only a weak
   reference, so a task nobody holds can vanish. We keep strong references.
2. **Exceptions disappear.** An unretrieved task exception surfaces only as a
   "Task exception was never retrieved" warning at GC time. We attach a done
   callback that logs it properly.
3. **Concurrency is unbounded.** A hundred simultaneous uploads would open a
   hundred concurrent OpenAI calls and a hundred database sessions. A semaphore
   caps in-flight pipelines at ``PROCESSING_MAX_CONCURRENCY``; the rest queue.

On shutdown, :meth:`drain` gives running work a grace period to finish so a
deploy does not strand documents in ``PROCESSING``.

Scaling boundary - stated plainly
---------------------------------
This is deliberately in-process: it needs no broker, which is the right
trade-off up to a single modest deployment. Its limits are real, and the design
accounts for them rather than hiding them:

* work does not survive a process restart - mitigated by the startup sweep in
  :func:`app.services.document_processor.recover_stuck_documents`, which returns
  abandoned documents to ``FAILED`` so they can be retried;
* work is not distributed across replicas.

Because the pipeline's entry point is just ``process_document(document_id)``
reading state from the database, moving to Celery, RQ, or an SQS consumer means
changing *how it is invoked*, not the pipeline itself.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.config import settings
from app.core.logging import request_id_ctx

logger = logging.getLogger(__name__)


class BackgroundTaskRunner:
    """Bounded, observable fire-and-forget task execution."""

    def __init__(self, max_concurrency: int | None = None) -> None:
        self._max_concurrency = max_concurrency or settings.PROCESSING_MAX_CONCURRENCY
        self._semaphore: asyncio.Semaphore | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closing = False

    def _get_semaphore(self) -> asyncio.Semaphore:
        # Created lazily: an asyncio.Semaphore must be instantiated on the loop
        # that will use it, and this object is built at import time.
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._max_concurrency)
        return self._semaphore

    @property
    def in_flight(self) -> int:
        return len(self._tasks)

    def submit(
        self,
        coro_factory: Callable[[], Awaitable[Any]],
        *,
        name: str,
    ) -> asyncio.Task[Any] | None:
        """Schedule work to run after the current response is sent.

        Takes a *factory* rather than a coroutine so nothing is created if the
        runner is shutting down - an un-awaited coroutine would emit a
        "never awaited" warning.
        """
        if self._closing:
            logger.warning("task_rejected_shutting_down", extra={"task_name": name})
            return None

        # Propagate the request id into the task so its log lines correlate with
        # the upload that triggered it. Contextvars do not cross create_task
        # unless captured explicitly like this.
        request_id = request_id_ctx.get()

        async def _runner() -> None:
            if request_id is not None:
                request_id_ctx.set(request_id)
            async with self._get_semaphore():
                await coro_factory()

        task = asyncio.create_task(_runner(), name=name)
        self._tasks.add(task)
        task.add_done_callback(self._on_done)
        return task

    def _on_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            logger.warning("task_cancelled", extra={"task_name": task.get_name()})
            return
        exc = task.exception()
        if exc is not None:
            # The pipeline handles its own errors, so anything arriving here is a
            # genuine bug worth a full traceback.
            logger.error(
                "task_failed",
                exc_info=exc,
                extra={"task_name": task.get_name()},
            )

    async def drain(self, timeout: float = 20.0) -> None:
        """Stop accepting work and wait (briefly) for what is running."""
        self._closing = True
        if not self._tasks:
            return

        pending = list(self._tasks)
        logger.info("draining_background_tasks", extra={"count": len(pending)})
        done, still_running = await asyncio.wait(pending, timeout=timeout)

        if still_running:
            logger.warning(
                "background_tasks_abandoned",
                extra={"count": len(still_running), "finished": len(done)},
            )
            for task in still_running:
                task.cancel()
            # Give cancellation a moment to propagate so `finally` blocks run and
            # database sessions close cleanly.
            await asyncio.gather(*still_running, return_exceptions=True)

    def reopen(self) -> None:
        """Allow submissions again - used between test cases."""
        self._closing = False
        self._semaphore = None


# Process-wide runner. The document endpoints submit to this; the application
# lifespan drains it.
task_runner = BackgroundTaskRunner()
