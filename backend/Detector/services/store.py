"""Where a submitted case lives between `202 Accepted` and the caller collecting it.

One `CaseRecord` per submission, and any number of watchers following it as it changes.
Two things make that more than a dictionary, and both come from the same fact: the
caller that submitted a case is not necessarily connected when the interesting things
happen.

*Late subscribers.* A browser POSTs the files, gets a case id, and only then opens the
websocket. By then three documents may already be read. So every record is a complete
snapshot and a watcher gets the current one immediately — connecting late loses nothing,
and neither does reconnecting.

*Several watchers at once.* Waking them is a broadcast, done by replacing an
`asyncio.Event` rather than setting and clearing one. With a single shared event,
whichever watcher woke first would clear it and the rest would sleep through the update.

Records are held in memory, which makes this a single-process store. Swapping in a
shared one is a change here and in `Detector.api.app`, and nowhere else.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from Detector.core.config import Settings, get_settings
from Detector.models.enums import JobStatus
from Detector.models.jobs import CaseEvent, CaseRecord
from Detector.models.reconciliation import CaseResult
from Detector.services.deps import StageEvent


class CaseExists(ValueError):
    """A case is already registered under that id.

    Refusing is the safe answer: overwriting would destroy a report the first caller has
    not collected yet, and there is no way from here to tell a retry from a clash.
    """

    def __init__(self, case_id: str, status: str) -> None:
        super().__init__(f'case {case_id!r} already exists and is {status}')
        self.case_id = case_id


class CaseNotFound(KeyError):
    """No case with that id, or it has aged out of the retention window."""

    def __init__(self, case_id: str) -> None:
        super().__init__(case_id)
        self.case_id = case_id

    def __str__(self) -> str:
        return f'no case {self.case_id!r}; it may have expired'


@dataclass(slots=True)
class _LiveCase:
    """A stored record plus the handle watchers wait on."""

    record: CaseRecord

    changed: asyncio.Event = field(default_factory=asyncio.Event)
    """Replaced, never cleared. A watcher captures the current event *before* reading the
    record, so an update landing between its read and its wait sets the event it is about
    to wait on and it wakes immediately rather than missing the change."""


class CaseStore:
    """Submitted cases, held until they are collected or expire.

    Bounded two ways, because neither alone is enough. `case_retention_seconds` drops
    finished cases nobody collected — a record holds a whole presentation's extracted
    contents, so it should not outlive the caller's interest in it. A duration bounds
    nothing under load, so `max_retained_cases` then evicts the oldest finished ones.
    Running cases are never evicted.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        settings = settings or get_settings()
        self._cases: dict[str, _LiveCase] = {}
        self._retention = timedelta(seconds=settings.case_retention_seconds)
        self._max_retained = settings.max_retained_cases
        self._lock = asyncio.Lock()

    async def create(self, case_id: str, *, document_count: int) -> CaseRecord:
        """Register a newly submitted case.

        Raises:
            CaseExists: that id is already held. Finished cases count — they are still
                collectable until they expire. `forget()` frees an id for reuse.
        """
        async with self._lock:
            self._evict()
            if case_id in self._cases:
                raise CaseExists(case_id, self._cases[case_id].record.status.value)
            live = _LiveCase(record=CaseRecord(case_id=case_id, document_count=document_count))
            self._cases[case_id] = live
            return live.record

    async def get(self, case_id: str) -> CaseRecord:
        """The current record."""
        async with self._lock:
            return self._live(case_id).record

    async def watch(self, case_id: str) -> AsyncIterator[CaseRecord]:
        """Yield the record now, and again after every change, until it is terminal.

        Each yield is the whole state, not a delta, so a client renders the latest one
        and is correct whenever it connected. Iteration ends at a terminal state, which
        lets a websocket simply run the loop to completion and close.
        """
        while True:
            async with self._lock:
                live = self._live(case_id)
                waiter, record = live.changed, live.record

            yield record
            if record.is_terminal:
                return
            await waiter.wait()

    async def mark_running(self, case_id: str) -> None:
        """A slot came free and the run has begun."""
        await self._update(case_id, status=JobStatus.RUNNING)

    async def append_event(self, case_id: str, event: StageEvent) -> None:
        """Add one audit event and wake every watcher."""
        async with self._lock:
            live = self._live(case_id)
            entry = CaseEvent(
                stage=event.stage,
                message=event.message,
                at=event.at,
                document_id=event.document_id,
                document_type=event.document_type,
            )
            self._publish(live, events=[*live.record.events, entry])

    async def succeed(self, case_id: str, result: CaseResult) -> None:
        """The run finished and produced a report."""
        await self._update(
            case_id, status=JobStatus.SUCCEEDED, finished_at=datetime.now(UTC), result=result
        )

    async def fail(self, case_id: str, *, code: str, detail: str) -> None:
        """The run stopped on an error."""
        await self._update(
            case_id,
            status=JobStatus.FAILED,
            finished_at=datetime.now(UTC),
            error=detail,
            error_code=code,
        )

    async def cancel(self, case_id: str) -> None:
        """The run was abandoned, by the caller or by the process shutting down."""
        await self._update(
            case_id,
            status=JobStatus.CANCELLED,
            finished_at=datetime.now(UTC),
            error='the analysis was cancelled',
            error_code='cancelled',
        )

    async def forget(self, case_id: str) -> None:
        """Drop a case now rather than at expiry, and release any watchers."""
        async with self._lock:
            live = self._cases.pop(case_id, None)
            if live is not None:
                live.changed.set()

    # --- internals -----------------------------------------------------------

    def _live(self, case_id: str) -> _LiveCase:
        try:
            return self._cases[case_id]
        except KeyError as exc:
            raise CaseNotFound(case_id) from exc

    async def _update(self, case_id: str, **fields: object) -> None:
        async with self._lock:
            self._publish(self._live(case_id), **fields)

    def _publish(self, live: _LiveCase, **fields: object) -> None:
        """Replace the record with an updated copy and wake everyone watching.

        Records are replaced rather than mutated so that one already handed to a watcher
        stays exactly what that watcher was shown.
        """
        live.record = live.record.model_copy(update=fields)
        waiters, live.changed = live.changed, asyncio.Event()
        waiters.set()

    def _evict(self) -> None:
        """Drop expired cases, then the oldest finished ones if too many remain.

        Called from `create` rather than a background sweeper: eviction only matters when
        the store is being used, and a sweeper is one more task to own and cancel.
        """
        cutoff = datetime.now(UTC) - self._retention
        finished = sorted(
            (live.record.finished_at, case_id)
            for case_id, live in self._cases.items()
            if live.record.finished_at is not None
        )
        overflow = len(self._cases) - self._max_retained
        for position, (finished_at, case_id) in enumerate(finished):
            if finished_at < cutoff or position < overflow:
                del self._cases[case_id]
