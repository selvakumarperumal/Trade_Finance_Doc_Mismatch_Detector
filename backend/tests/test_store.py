"""The case store: replay for late subscribers, broadcast to several, and the bounds."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from Detector.core.config import Settings
from Detector.models.enums import JobStatus
from Detector.models.jobs import CaseRecord
from Detector.services.deps import StageEvent
from Detector.services.store import CaseExists, CaseNotFound, CaseStore

pytestmark = pytest.mark.asyncio


def event(message: str = 'read') -> StageEvent:
    return StageEvent(stage='ocr', message=message, at=datetime.now(UTC), document_id='doc-1')


async def collect(store: CaseStore, case_id: str, out: list) -> None:
    async for record in store.watch(case_id):
        out.append(record)


async def test_unknown_case_is_distinguishable_from_a_failed_one(settings: Settings) -> None:
    store = CaseStore(settings)
    with pytest.raises(CaseNotFound):
        await store.get('nope')


async def test_an_id_in_use_is_refused(settings: Settings) -> None:
    store = CaseStore(settings)
    await store.create('c1', document_count=1)
    with pytest.raises(CaseExists):
        await store.create('c1', document_count=1)


async def test_a_finished_case_still_holds_its_id(settings: Settings) -> None:
    """Overwriting one would destroy a report the first caller has not collected."""
    store = CaseStore(settings)
    await store.create('c1', document_count=1)
    await store.fail('c1', code='model_unavailable', detail='provider down')

    with pytest.raises(CaseExists):
        await store.create('c1', document_count=1)

    await store.forget('c1')
    assert (await store.create('c1', document_count=1)).status is JobStatus.QUEUED


async def test_a_late_subscriber_is_replayed_everything_it_missed(settings: Settings) -> None:
    store = CaseStore(settings)
    await store.create('c1', document_count=1)
    await store.mark_running('c1')
    for index in range(3):
        await store.append_event('c1', event(f'read {index}'))

    seen: list[CaseRecord] = []
    watcher = asyncio.create_task(collect(store, 'c1', seen))
    await asyncio.sleep(0)
    await store.succeed('c1', _result())
    await asyncio.wait_for(watcher, timeout=2)

    assert [e.message for e in seen[0].events] == ['read 0', 'read 1', 'read 2'], (
        'the first snapshot must already carry everything that happened before connecting'
    )
    assert seen[-1].status is JobStatus.SUCCEEDED


async def test_every_watcher_is_woken_not_just_the_first(settings: Settings) -> None:
    """A shared, cleared event would let whichever watcher woke first starve the rest."""
    store = CaseStore(settings)
    await store.create('c1', document_count=1)

    first: list[CaseRecord] = []
    second: list[CaseRecord] = []
    watchers = [
        asyncio.create_task(collect(store, 'c1', first)),
        asyncio.create_task(collect(store, 'c1', second)),
    ]
    await asyncio.sleep(0)
    await store.append_event('c1', event())
    await store.succeed('c1', _result())
    await asyncio.wait_for(asyncio.gather(*watchers), timeout=2)

    assert [e.message for e in first[-1].events] == ['read']
    assert [e.message for e in second[-1].events] == ['read']
    assert first[-1].status is second[-1].status is JobStatus.SUCCEEDED


async def test_watching_ends_when_the_case_does(settings: Settings) -> None:
    store = CaseStore(settings)
    await store.create('c1', document_count=1)
    await store.cancel('c1')

    seen = [record async for record in store.watch('c1')]
    assert [r.status for r in seen] == [JobStatus.CANCELLED]


async def test_expired_cases_are_dropped(settings: Settings) -> None:
    store = CaseStore(settings.model_copy(update={'case_retention_seconds': 0}))
    await store.create('old', document_count=1)
    await store.fail('old', code='x', detail='y')
    # Backdate it past the (zero-second) window, then trigger eviction with a write.
    store._cases['old'].record = store._cases['old'].record.model_copy(
        update={'finished_at': datetime.now(UTC) - timedelta(seconds=5)}
    )
    await store.create('new', document_count=1)

    with pytest.raises(CaseNotFound):
        await store.get('old')
    assert (await store.get('new')).case_id == 'new'


async def test_a_running_case_is_never_evicted(settings: Settings) -> None:
    store = CaseStore(settings.model_copy(update={'max_retained_cases': 1}))
    await store.create('running', document_count=1)
    await store.mark_running('running')
    await store.create('other', document_count=1)

    assert (await store.get('running')).status is JobStatus.RUNNING


def _result():
    from Detector.models.enums import CaseStatus
    from Detector.models.reconciliation import CaseResult, ReconciliationReport

    now = datetime.now(UTC)
    return CaseResult(
        case_id='c1',
        status=CaseStatus.CLEAN,
        report=ReconciliationReport(status=CaseStatus.CLEAN, summary='fine'),
        started_at=now,
        completed_at=now,
    )
