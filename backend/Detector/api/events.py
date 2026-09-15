"""The Socket.IO layer: watching a case as it runs.

Submitting a case and collecting its answer are separate acts, because the analysis takes
minutes. `GET /v1/cases/{id}` is the polling half; this is the pushing half.

    client                                   server
      |  connect  /socket.io                   |
      |  emit 'subscribe' {case_id}            |
      |                                        |  runner.watch(case_id)
      |  <-- 'case'   {whole CaseRecord}       |  once immediately, then on every change
      |  <-- 'case'   {whole CaseRecord}       |
      |  <-- 'done'   {case_id}                |  the case reached a terminal status
      |  emit 'unsubscribe' {case_id}          |  optional; disconnecting does it too

Every `case` event carries the **whole record** — the same shape `GET /v1/cases/{id}`
returns — rather than a delta. A client renders the latest one and is correct whether it
subscribed before the first document was read or after the last, so there is no race
between the POST returning and the socket opening, and a reconnect needs no cursor.

One task per subscription does the pumping, and a client may hold several at once. The
tasks are tracked per session so that a disconnect cancels them, rather than leaving them
emitting into a socket nobody is reading.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import socketio
from fastapi import FastAPI

from Detector.services.runner import CaseRunner
from Detector.services.store import CaseNotFound

logger = logging.getLogger(__name__)

MOUNT_PATH = '/socket.io'
"""Where the endpoint is mounted — the path a Socket.IO client looks for by default."""

SUBSCRIBE = 'subscribe'
"""Client -> server: `{'case_id': '...'}`, start streaming that case."""

UNSUBSCRIBE = 'unsubscribe'
"""Client -> server: `{'case_id': '...'}`, stop streaming it."""

CASE = 'case'
"""Server -> client: one whole `CaseRecord`, on subscribe and after every change."""

DONE = 'done'
"""Server -> client: that case reached a terminal status; no more `case` events."""

ERROR = 'error'
"""Server -> client: `{'code': ..., 'detail': ...}`, the same codes the HTTP layer uses."""

type Emit = Callable[[str, dict[str, Any]], Awaitable[None]]
"""How a stream sends one event. Keeping this a parameter is what lets the streaming
logic be tested without a Socket.IO server in the way."""


async def stream_case(runner: CaseRunner, case_id: str, emit: Emit) -> None:
    """Push every version of one case's record through `emit`, until it is terminal.

    Ends by itself: `runner.watch` stops iterating at a terminal status, which is when
    `done` goes out. Cancellation is the other way it ends, and it is left to propagate
    so the task really does stop.
    """
    try:
        async for record in runner.watch(case_id):
            await emit(CASE, record.model_dump(mode='json'))
        await emit(DONE, {'case_id': case_id})
    except CaseNotFound as exc:
        await emit(ERROR, {'code': 'case_not_found', 'detail': str(exc)})
    except asyncio.CancelledError:
        raise  # The client went away, or the server is shutting down.
    except Exception as exc:  # noqa: BLE001 - a subscription must not kill the connection
        logger.exception('streaming case %s failed', case_id)
        await emit(ERROR, {'code': 'internal_error', 'detail': f'{type(exc).__name__}: {exc}'})


def build_socket_app(app: FastAPI, cors_origins: list[str] | None = None) -> socketio.ASGIApp:
    """The Socket.IO server, as an ASGI app to mount on `app` at `MOUNT_PATH`.

    The runner is read off `app.state` at event time rather than captured now, because
    the lifespan handler builds it after this app is assembled — the same reason
    `api/dependencies.py` reads it that way.

    Socket.IO is a separate ASGI app, so FastAPI's `CORSMiddleware` never sees its
    requests; it does its own origin check from `cors_origins`.

    No configured origins becomes `None`, not `[]`. They are not the same: `None` means
    same-origin only, while `[]` rejects *every* request that carries an `Origin` header
    — and the polling transport POSTs always carry one, so `[]` would lock out a frontend
    served from this very host.
    """
    server = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins=cors_origins or None)

    streams: dict[str, dict[str, asyncio.Task[None]]] = {}
    """Running subscriptions, as `{session id: {case id: task}}`."""

    def sender(sid: str) -> Emit:
        """An `Emit` that sends to one session."""

        async def emit(event: str, payload: dict[str, Any]) -> None:
            await server.emit(event, payload, to=sid)

        return emit

    def stop(sid: str, case_id: str) -> None:
        """Cancel one subscription, if it is running."""
        task = streams.get(sid, {}).pop(case_id, None)
        if task is not None:
            task.cancel()

    @server.event
    async def subscribe(sid: str, data: Any) -> None:
        """Start streaming one case to this client."""
        case_id = data.get('case_id') if isinstance(data, dict) else None
        if not isinstance(case_id, str) or not case_id:
            await server.emit(
                ERROR,
                {'code': 'invalid_subscription', 'detail': "expected {'case_id': '...'}"},
                to=sid,
            )
            return

        runner = getattr(app.state, 'runner', None)
        if not isinstance(runner, CaseRunner):  # pragma: no cover - only before startup finishes
            await server.emit(
                ERROR,
                {'code': 'server_busy', 'detail': 'the detector is still starting up'},
                to=sid,
            )
            return

        stop(sid, case_id)  # Re-subscribing replaces the old stream rather than doubling it.
        task = asyncio.create_task(
            stream_case(runner, case_id, sender(sid)), name=f'stream:{sid}:{case_id}'
        )
        streams.setdefault(sid, {})[case_id] = task
        # Drop it from the table once it ends on its own, so a long-lived connection
        # watching many cases does not accumulate finished tasks.
        task.add_done_callback(lambda _: streams.get(sid, {}).pop(case_id, None))

    @server.event
    async def unsubscribe(sid: str, data: Any) -> None:
        """Stop streaming one case to this client."""
        case_id = data.get('case_id') if isinstance(data, dict) else None
        if isinstance(case_id, str) and case_id:
            stop(sid, case_id)

    @server.event
    async def disconnect(sid: str) -> None:
        """Cancel everything this client was watching."""
        for task in streams.pop(sid, {}).values():
            task.cancel()

    # `socketio_path=''` because Starlette strips the mount prefix before the sub-app
    # sees the request, so the path left to match is empty.
    return socketio.ASGIApp(server, socketio_path='')
