"""Running submitted cases in the background, and knowing when to say no.

`DetectorPipeline` analyses one presentation and takes as long as it takes. The runner
turns that into something an HTTP request can hand off: it accepts a case, returns
immediately, and runs the analysis on a task that outlives the request.

Two ceilings, answering different questions. `max_concurrent` is how many analyses run
at once — the model-call limiter in `Detector.services.agents` bounds *requests*, but a
running case also holds uploaded bytes and per-document state in memory, and bounding
runs is what keeps that flat. `max_queued` is how many submissions may wait: without it
an overload becomes an unbounded backlog in which every caller waits and none is told.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Final

from pydantic_ai.exceptions import (
    AgentRunError,
    ConcurrencyLimitExceeded,
    RunCancelled,
    UsageLimitExceeded,
)

from Detector.core.config import Settings, get_settings
from Detector.core.observability import span
from Detector.models.documents import CaseInput
from Detector.models.jobs import CaseRecord
from Detector.services.deps import StageEvent
from Detector.services.pipeline import DetectorPipeline
from Detector.services.store import CaseNotFound, CaseStore

SHUTDOWN_GRACE_SECONDS: Final = 10.0
"""How long a shutting-down process waits for running cases before cancelling them."""


class TooBusy(Exception):
    """The runner is at its queue ceiling and is refusing new submissions."""


@dataclass(eq=False)
class CaseRunner:
    """Accepts cases, runs them against the pipeline, records what happens.

    It owns the set of in-flight tasks, so it has a lifecycle: `aclose()` is what the
    FastAPI lifespan calls on the way out.
    """

    pipeline: DetectorPipeline
    store: CaseStore
    max_concurrent: int = 4
    max_queued: int = 64

    _slots: asyncio.Semaphore = field(init=False, repr=False)
    _tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False, repr=False)
    _waiting: int = field(default=0, init=False, repr=False)
    _closing: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self._slots = asyncio.Semaphore(self.max_concurrent)

    @classmethod
    def build(
        cls,
        pipeline: DetectorPipeline,
        store: CaseStore | None = None,
        settings: Settings | None = None,
    ) -> CaseRunner:
        """A runner on the configured ceilings."""
        settings = settings or get_settings()
        return cls(
            pipeline=pipeline,
            store=store or CaseStore(settings),
            max_concurrent=settings.max_concurrent_cases,
            max_queued=settings.max_queued_cases,
        )

    @property
    def in_flight(self) -> int:
        """Cases currently being analysed or waiting for a slot."""
        return len(self._tasks)

    async def submit(self, case: CaseInput) -> CaseRecord:
        """Accept a case and return its record straight away.

        The record is `queued`; the run continues on a background task. Follow it with
        `store.watch(case_id)`, or by polling `store.get(case_id)`.

        Raises:
            TooBusy: too many submissions are already waiting for a slot.
            CaseExists: a case is already registered under this id.
        """
        if self._closing:
            raise TooBusy('the service is shutting down and is not accepting cases')
        if self._waiting >= self.max_queued:
            raise TooBusy(
                f'{self._waiting} case(s) already waiting, at the limit of {self.max_queued}'
            )

        record = await self.store.create(case.case_id, document_count=len(case.documents))
        self._waiting += 1

        task = asyncio.create_task(self._run(case), name=f'case:{case.case_id}')
        # Hold a reference until the task ends, or the loop may collect it mid-analysis.
        self._tasks.add(task)
        task.add_done_callback(self._retire)
        return record

    async def cancel(self, case_id: str) -> bool:
        """Stop a running case. Returns whether there was one to stop."""
        for task in tuple(self._tasks):
            if task.get_name() == f'case:{case_id}':
                task.cancel()
                return True
        return False

    async def aclose(self, grace: float = SHUTDOWN_GRACE_SECONDS) -> None:
        """Stop accepting cases, let the running ones finish, then cancel the rest.

        A case cancelled here is *recorded* as cancelled rather than left `running`, so a
        caller polling across a deploy is told what happened instead of waiting on a
        record that will never change.
        """
        self._closing = True
        if not self._tasks:
            return
        _, unfinished = await asyncio.wait(tuple(self._tasks), timeout=grace)
        for task in unfinished:
            task.cancel()
        if unfinished:
            await asyncio.gather(*unfinished, return_exceptions=True)

    # --- internals -----------------------------------------------------------

    def _retire(self, task: asyncio.Task[None]) -> None:
        """Drop a finished task, and read whatever it failed with.

        Reading the exception matters even though `_run` handles its own failures: an
        exception nobody retrieves is reported by the event loop at collection time as
        "Task exception was never retrieved", which turns any gap in the handling above
        into noise in the logs rather than something anyone can act on.
        """
        self._tasks.discard(task)
        if not task.cancelled() and (exception := task.exception()) is not None:
            with span('case task failed unexpectedly: {error}', error=repr(exception)):
                pass

    async def _run(self, case: CaseInput) -> None:
        """Wait for a slot, run the case, and record the outcome exactly once."""
        admitted = False
        try:
            async with self._slots:
                admitted = True
                self._waiting -= 1
                await self.store.mark_running(case.case_id)
                await self._analyse(case)
        except CaseNotFound:
            # The record was deleted while the run was in flight. There is nothing left
            # to report the outcome to, and the caller asked for exactly that, so this
            # is a normal end rather than a failure.
            return
        except asyncio.CancelledError:
            # Cancellation has already been delivered, so this await runs normally; it is
            # what turns a killed task into a case the caller can see the end of.
            with suppress(Exception):
                await self.store.cancel(case.case_id)
            raise
        finally:
            if not admitted:
                # Cancelled while still queued, so the accounting above never ran.
                self._waiting -= 1

    async def _analyse(self, case: CaseInput) -> None:
        """Run one case, translating failures into a recorded terminal state.

        Everything reaching here is a case-level failure; a per-document problem was
        already degraded inside the graph. A case must always end somewhere, or a caller
        polls it forever — which is why the last clause catches everything.
        """

        async def on_event(event: StageEvent) -> None:
            await self.store.append_event(case.case_id, event)

        try:
            result = await self.pipeline.run_with_progress(case, on_event)
        except ValueError as exc:
            await self.store.fail(case.case_id, code='invalid_case', detail=str(exc))
        except UsageLimitExceeded as exc:
            await self.store.fail(
                case.case_id,
                code='usage_limit_exceeded',
                detail=f'the case exceeded its configured model budget: {exc}',
            )
        except ConcurrencyLimitExceeded as exc:
            await self.store.fail(
                case.case_id,
                code='server_busy',
                detail=f'the model concurrency limit was hit: {exc}',
            )
        except RunCancelled:
            await self.store.cancel(case.case_id)
        except AgentRunError as exc:
            await self.store.fail(
                case.case_id, code='model_unavailable', detail=f'the model provider failed: {exc}'
            )
        except Exception as exc:  # noqa: BLE001 - a case must always reach a terminal state
            await self.store.fail(
                case.case_id, code='internal_error', detail=f'{type(exc).__name__}: {exc}'
            )
        else:
            await self.store.succeed(case.case_id, result)
