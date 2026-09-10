"""The runner: what it accepts, what it refuses, and how a case always reaches an end."""

from __future__ import annotations

import asyncio

import pytest

from Detector.core.config import Settings
from Detector.models.documents import CaseInput, RawDocument
from Detector.models.enums import JobStatus
from Detector.services.pipeline import DetectorPipeline
from Detector.services.runner import CaseRunner, TooBusy
from Detector.services.store import CaseStore

pytestmark = pytest.mark.asyncio


def a_case(case_id: str, documents: int = 1) -> CaseInput:
    return CaseInput(
        case_id=case_id,
        documents=[
            RawDocument(document_id=f'doc-{i}', text=f'COMMERCIAL INVOICE {i}')
            for i in range(documents)
        ],
    )


async def finish(runner: CaseRunner, case_id: str) -> None:
    async for _ in runner.store.watch(case_id):
        pass


@pytest.fixture
def runner(settings: Settings) -> CaseRunner:
    return CaseRunner.build(DetectorPipeline.default(settings), CaseStore(settings), settings)


async def test_submitting_returns_before_the_work_is_done(runner: CaseRunner) -> None:
    record = await runner.submit(a_case('c1'))
    assert record.status is JobStatus.QUEUED
    assert record.result is None

    await asyncio.wait_for(finish(runner, 'c1'), timeout=5)
    assert (await runner.store.get('c1')).status is JobStatus.SUCCEEDED
    await runner.aclose(grace=1)


async def test_a_full_queue_is_refused_rather_than_silently_backlogged(
    settings: Settings,
) -> None:
    runner = CaseRunner(
        pipeline=DetectorPipeline.default(settings),
        store=CaseStore(settings),
        max_concurrent=1,
        max_queued=1,
    )
    await runner.submit(a_case('q1'))
    with pytest.raises(TooBusy):
        await runner.submit(a_case('q2'))
    await runner.aclose(grace=5)


async def test_a_failing_run_is_recorded_not_lost(runner: CaseRunner) -> None:
    """A case must always reach a terminal state, or a caller polls it forever."""

    async def explode(case, on_event):
        raise RuntimeError('the wheels came off')

    runner.pipeline = type('Broken', (), {'run_with_progress': staticmethod(explode)})()
    await runner.submit(a_case('boom'))
    await asyncio.wait_for(finish(runner, 'boom'), timeout=5)

    record = await runner.store.get('boom')
    assert record.status is JobStatus.FAILED
    assert record.error_code == 'internal_error'
    assert 'the wheels came off' in (record.error or '')
    await runner.aclose(grace=1)


async def test_an_invalid_case_fails_with_the_same_code_the_api_uses(
    settings: Settings,
) -> None:
    """`ingest` rejects an over-large case; the code matches the synchronous 422."""
    small = settings.model_copy(update={'max_documents_per_case': 1})
    runner = CaseRunner.build(DetectorPipeline.default(small), CaseStore(small), small)

    await runner.submit(a_case('too-big', documents=3))
    await asyncio.wait_for(finish(runner, 'too-big'), timeout=5)

    record = await runner.store.get('too-big')
    assert record.status is JobStatus.FAILED
    assert record.error_code == 'invalid_case'
    await runner.aclose(grace=1)


async def test_shutdown_ends_running_cases_instead_of_stranding_them(
    settings: Settings,
) -> None:
    started = asyncio.Event()

    async def never_finishes(case, on_event):
        started.set()
        await asyncio.sleep(3600)

    runner = CaseRunner.build(DetectorPipeline.default(settings), CaseStore(settings), settings)
    runner.pipeline = type('Slow', (), {'run_with_progress': staticmethod(never_finishes)})()

    await runner.submit(a_case('stuck'))
    await asyncio.wait_for(started.wait(), timeout=5)
    await runner.aclose(grace=0.05)

    record = await runner.store.get('stuck')
    assert record.status is JobStatus.CANCELLED, 'a caller polling across a deploy must be told'
    assert runner.in_flight == 0


async def test_it_stops_accepting_once_closed(runner: CaseRunner) -> None:
    await runner.aclose(grace=1)
    with pytest.raises(TooBusy, match='shutting down'):
        await runner.submit(a_case('late'))
