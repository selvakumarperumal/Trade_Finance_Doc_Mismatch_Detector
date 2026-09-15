"""The FastAPI application: what it builds at startup, and how it lets go at shutdown.

Startup does three things, in an order that matters:

1. load the prompts, so a typo in `Config/prompts.yaml` fails here rather than on the
   first request;
2. open the Textract client the OCR step shares for the life of the process;
3. build the runner that owns in-flight cases.

OCR is the one part allowed to fail. A deployment that only ever receives extracted text
has no reason to hold AWS credentials, so a client that cannot be opened leaves the
service running with `ocr_available: false` instead of refusing to start.

Shutdown is deliberately not the mirror image. Cases run on background tasks that outlive
the request that submitted them, so the runner gets a grace period to finish what it is
holding — and whatever is still running when that expires is cancelled and *recorded* as
cancelled, so a caller polling across a deploy is told what happened rather than left
watching a case that will never change.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from Detector.api.errors import register_error_handlers
from Detector.api.events import MOUNT_PATH, build_socket_app
from Detector.api.routes import cases_router, system_router
from Detector.core.config import Settings, get_settings
from Detector.prompts.registry import get_prompts
from Detector.services.pipeline import DetectorPipeline
from Detector.services.runner import CaseRunner

VERSION = '0.1.0'

logger = logging.getLogger(__name__)

DESCRIPTION = """
Cross-checks the documents presented under a letter of credit — the credit itself, the
invoice, the bill of lading and the rest — and reports the discrepancies a bank ops
examiner would raise under UCP 600, with a verdict of `clean`, `needs_review` or
`blocked`.

**Submitting and collecting are separate.** Analysis takes minutes, so a submission
answers `202` with a case id and the run continues in the background.

1. `POST /v1/cases/uploads` with the files, or `POST /v1/cases` with extracted text.
2. Connect a **Socket.IO** client to `/socket.io` and emit `subscribe` with that case id
   to watch each document be read, classified and extracted — it replays whatever
   already happened, so subscribing late loses nothing. Or poll
   `GET /v1/cases/{case_id}` until `status` is `succeeded`.
3. `DELETE /v1/cases/{case_id}` when you are done with it, to drop what it held.
"""


async def _open_pipeline(stack: AsyncExitStack, settings: Settings) -> DetectorPipeline:
    """The pipeline, with OCR if this deployment can have it."""
    if not settings.ocr_enabled:
        return DetectorPipeline.default(settings)
    try:
        return await stack.enter_async_context(DetectorPipeline.open(settings))
    except (BotoCoreError, ClientError) as exc:
        logger.warning('Textract unavailable, continuing without OCR: %s', exc)
        return DetectorPipeline.default(settings)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build everything the routes need once, and tear it down on the way out.

    The settings come off `app.state`, where `create_app` put them, so an app built with
    an explicit `Settings` really does start up under that configuration.
    """
    settings: Settings = getattr(app.state, 'settings', None) or get_settings()
    get_prompts()

    async with AsyncExitStack() as stack:
        pipeline = await _open_pipeline(stack, settings)
        runner = CaseRunner.build(pipeline, settings=settings)
        # The stack unwinds in reverse, so registering this last means the runner closes
        # first: a case still being read keeps a live Textract client to finish or fail
        # with, rather than losing it mid-document.
        stack.push_async_callback(runner.aclose)

        app.state.runner = runner
        yield


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application. Call it once per process, or once per test."""
    settings = settings or get_settings()

    app = FastAPI(
        title='Trade Finance Document Mismatch Detector',
        description=DESCRIPTION,
        version=VERSION,
        lifespan=lifespan,
    )
    app.state.settings = settings

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=['GET', 'POST', 'DELETE'],
            allow_headers=['*'],
            # A browser cannot read a response header unless it is exposed, and this is
            # the one a frontend needs: it says how long to wait after a 503.
            expose_headers=['Retry-After'],
        )

    register_error_handlers(app)
    app.include_router(system_router)
    app.include_router(cases_router)

    # Socket.IO is its own ASGI app rather than a route, so it is mounted rather than
    # included — and it does its own origin check, because the CORS middleware above
    # never sees requests that land inside a mount.
    app.mount(MOUNT_PATH, build_socket_app(app, settings.cors_origins))

    # Last, because a mount at '/' matches anything the routes above did not: the API
    # keeps its paths and the frontend gets everything else. `html=True` serves
    # index.html for '/' and falls back to it for unknown paths.
    if settings.frontend_dir is not None and settings.frontend_dir.is_dir():
        app.mount('/', StaticFiles(directory=settings.frontend_dir, html=True), name='frontend')

    return app
