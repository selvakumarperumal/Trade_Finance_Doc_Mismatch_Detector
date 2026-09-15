"""The Socket.IO layer: what a subscriber is sent, and when.

Most of these drive `stream_case` directly with a list-collecting `emit`, which is what
the transport-free signature is for. The last one is the exception: it runs the real app
under uvicorn and connects a real Socket.IO client, because the mount path and the
handler wiring are exactly the things a fake cannot check.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from typing import Any

import pytest
import socketio
import uvicorn

from Detector.api.app import create_app
from Detector.api.events import CASE, DONE, ERROR, MOUNT_PATH, stream_case
from Detector.core.config import Settings
from Detector.services.pipeline import DetectorPipeline
from Detector.services.runner import CaseRunner
from Detector.services.store import CaseStore

pytestmark = pytest.mark.asyncio


class Recorder:
    """Stands in for the Socket.IO transport, keeping what was sent."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, event: str, payload: dict[str, Any]) -> None:
        self.sent.append((event, payload))

    @property
    def events(self) -> list[str]:
        return [event for event, _ in self.sent]


def build_runner(settings: Settings) -> CaseRunner:
    return CaseRunner.build(DetectorPipeline.default(settings), CaseStore(settings), settings)


async def test_a_subscriber_is_sent_the_record_then_done(settings: Settings) -> None:
    runner = build_runner(settings)
    await runner.store.create('c1', document_count=1)

    emit = Recorder()
    stream = asyncio.create_task(stream_case(runner, 'c1', emit))
    await asyncio.sleep(0)  # let the first record go out

    await runner.store.mark_running('c1')
    await runner.store.succeed('c1', _result())
    await asyncio.wait_for(stream, timeout=2)

    assert emit.events[0] == CASE, 'the current state goes out before any change'
    assert emit.events[-1] == DONE, 'a terminal status ends the stream'
    assert [p['status'] for e, p in emit.sent if e == CASE][-1] == 'succeeded'


async def test_subscribing_after_it_finished_still_gets_the_whole_thing(
    settings: Settings,
) -> None:
    """A client that connects late loses nothing: every event is the full record."""
    runner = build_runner(settings)
    await runner.store.create('c1', document_count=1)
    await runner.store.succeed('c1', _result())

    emit = Recorder()
    await asyncio.wait_for(stream_case(runner, 'c1', emit), timeout=2)

    assert emit.events == [CASE, DONE]
    assert emit.sent[0][1]['status'] == 'succeeded'
    assert emit.sent[0][1]['result'] is not None


async def test_an_unknown_case_is_an_error_rather_than_a_hang(settings: Settings) -> None:
    emit = Recorder()
    await asyncio.wait_for(stream_case(build_runner(settings), 'ghost', emit), timeout=2)

    assert emit.sent == [
        (ERROR, {'code': 'case_not_found', 'detail': "no case 'ghost'; it may have expired"})
    ]


async def test_every_change_is_pushed_in_order(settings: Settings) -> None:
    runner = build_runner(settings)
    await runner.store.create('c1', document_count=2)

    emit = Recorder()
    stream = asyncio.create_task(stream_case(runner, 'c1', emit))
    await asyncio.sleep(0)

    for index in range(3):
        await runner.store.append_event('c1', _stage_event(f'read {index}'))
        await asyncio.sleep(0)
    await runner.store.cancel('c1')
    await asyncio.wait_for(stream, timeout=2)

    trails = [len(p['events']) for e, p in emit.sent if e == CASE]
    assert trails == sorted(trails), 'the audit trail only ever grows'
    assert trails[-1] == 3


async def test_cancelling_a_subscription_stops_it(settings: Settings) -> None:
    """A disconnect cancels the task; it must not keep emitting afterwards."""
    runner = build_runner(settings)
    await runner.store.create('c1', document_count=1)

    emit = Recorder()
    stream = asyncio.create_task(stream_case(runner, 'c1', emit))
    await asyncio.sleep(0)
    sent_before = len(emit.sent)

    stream.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stream

    await runner.store.succeed('c1', _result())
    await asyncio.sleep(0)
    assert len(emit.sent) == sent_before, 'a cancelled subscription sends nothing more'


# --- the real thing, over a real socket --------------------------------------


async def test_a_socketio_client_receives_a_case_over_the_wire(settings: Settings) -> None:
    """End to end: the mount path, the handlers, and a genuine Socket.IO client."""
    app = create_app(settings)
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error', lifespan='on')
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        await _wait_until_serving(server)

        submitted = await asyncio.to_thread(
            _post_case, f'http://127.0.0.1:{port}/v1/cases', {'text': 'COMMERCIAL INVOICE 1'}
        )
        case_id = submitted['case_id']

        client = socketio.AsyncClient()
        seen: list[tuple[str, Any]] = []
        finished = asyncio.Event()

        @client.on(CASE)
        def _case(payload: Any) -> None:
            seen.append((CASE, payload))

        @client.on(DONE)
        def _done(payload: Any) -> None:
            seen.append((DONE, payload))
            finished.set()

        await client.connect(f'http://127.0.0.1:{port}', socketio_path=MOUNT_PATH)
        await client.emit('subscribe', {'case_id': case_id})
        await asyncio.wait_for(finished.wait(), timeout=15)
        await client.disconnect()
    finally:
        server.should_exit = True
        await asyncio.to_thread(thread.join, 10)

    assert [event for event, _ in seen][-1] == DONE
    records = [payload for event, payload in seen if event == CASE]
    assert records, 'at least one whole record must arrive'
    assert records[-1]['case_id'] == case_id
    assert records[-1]['status'] == 'succeeded'
    assert records[-1]['result'] is not None


# --- helpers -----------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return int(sock.getsockname()[1])


async def _wait_until_serving(server: uvicorn.Server, timeout: float = 15.0) -> None:
    for _ in range(int(timeout / 0.05)):
        if server.started:
            return
        await asyncio.sleep(0.05)
    raise AssertionError('the test server never came up')


def _post_case(url: str, *documents: dict[str, str]) -> dict[str, Any]:
    """Submit a case over real HTTP. `urllib` rather than a client library, so this
    test pulls in nothing the project does not already depend on."""
    import json
    import urllib.request

    body = json.dumps({'documents': list(documents)}).encode()
    request = urllib.request.Request(
        url, data=body, headers={'Content-Type': 'application/json'}, method='POST'
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def _stage_event(message: str):
    from datetime import UTC, datetime

    from Detector.services.deps import StageEvent

    return StageEvent(stage='ocr', message=message, at=datetime.now(UTC), document_id='doc-1')


def _result():
    from datetime import UTC, datetime

    from Detector.models.enums import CaseStatus
    from Detector.models.reconciliation import CaseResult, ReconciliationReport

    return CaseResult(
        case_id='c1',
        status=CaseStatus.CLEAN,
        report=ReconciliationReport(status=CaseStatus.CLEAN, summary='nothing to report'),
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )
