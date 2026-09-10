"""The FastAPI application.

Startup does four things, in an order that matters: configure Logfire so the rest of
startup is traced, load the prompts so a typo in `Config/prompts.yaml` fails here rather
than on the first request, open the Textract client the OCR step shares for the life of
the process, and build the runner that owns in-flight cases.

OCR is the one part allowed to fail. A deployment that only ever receives extracted text
has no reason to hold AWS credentials, so a client that cannot be opened leaves the
service running with `ocr_available: false` instead of refusing to start.

Shutdown is not symmetric with startup, and deliberately so. Cases run on background
tasks that outlive the request that submitted them, so the runner is given a grace
period to finish what it is holding before anything is torn down — and whatever is still
running when that expires is cancelled and *recorded* as cancelled, so a caller polling
across a deploy is told what happened rather than left watching a case that will never
change.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from Detector.api.errors import register_error_handlers
from Detector.api.routes import cases_router, system_router
from Detector.core.config import Settings, get_settings
from Detector.core.observability import configure_observability, span
from Detector.prompts.registry import get_prompts
from Detector.services.pipeline import DetectorPipeline
from Detector.services.runner import CaseRunner
from Detector.services.store import CaseStore

DESCRIPTION = """
Cross-checks the documents presented under a letter of credit — the credit itself, the
invoice, the bill of lading and the rest — and reports the discrepancies a bank ops
examiner would raise under UCP 600, with a verdict of `clean`, `needs_review` or
`blocked`.

**Submitting and collecting are separate.** Analysis takes minutes, so a submission
answers `202` with a case id and the run continues in the background.

1. `POST /v1/cases/uploads` with the files, or `POST /v1/cases` with extracted text.
2. Open `WS /v1/cases/{case_id}/stream` to watch each document be read, classified and
   extracted — it replays whatever already happened, so connecting late loses nothing.
   Or poll `GET /v1/cases/{case_id}` until `status` is `succeeded`.
3. `DELETE /v1/cases/{case_id}` when you are done with it, to drop what it held.
"""


async def _open_pipeline(stack: AsyncExitStack, settings: Settings) -> DetectorPipeline:
    """The pipeline, with OCR if this deployment can have it."""
    if not settings.ocr_enabled:
        return DetectorPipeline.default(settings)
    try:
        return await stack.enter_async_context(DetectorPipeline.open(settings))
    except (BotoCoreError, ClientError) as exc:
        with span('textract unavailable, continuing without OCR: {reason}', reason=str(exc)):
            return DetectorPipeline.default(settings)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build everything the routes need once, and tear it down on the way out.

    The settings come off `app.state`, where `create_app` put them, so an app built with
    an explicit `Settings` really does start up under that configuration.
    """
    settings: Settings = getattr(app.state, 'settings', None) or get_settings()
    configure_observability(settings)
    get_prompts()

    async with AsyncExitStack() as stack:
        pipeline = await _open_pipeline(stack, settings)
        runner = CaseRunner.build(pipeline, CaseStore(settings), settings)
        # Registered before the yield so it runs before the Textract client closes:
        # a case still being read needs the client alive to finish or to fail cleanly.
        stack.push_async_callback(runner.aclose)

        app.state.runner = runner
        yield


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application. Call it once per process, or once per test."""
    settings = settings or get_settings()

    app = FastAPI(
        title='Trade Finance Document Mismatch Detector',
        description=DESCRIPTION,
        version=settings.logfire_service_version or '0.1.0',
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
    return app
