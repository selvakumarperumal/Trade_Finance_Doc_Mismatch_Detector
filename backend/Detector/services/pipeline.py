"""The entry point the API layer calls.

Nothing above this module needs to know about Pydantic AI or Pydantic Graph: hand it a
`CaseInput`, get back a `CaseResult`.

There are two ways in, and the difference is OCR. `get_pipeline()` is the cheap
process-wide one for presentations that already carry their text. `open()` is an async
context manager that additionally holds a Textract client open, so documents can arrive
as bytes; that is what the FastAPI lifespan uses.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import lru_cache

from Detector.core.config import Settings, get_settings
from Detector.models.documents import CaseInput
from Detector.models.reconciliation import CaseResult
from Detector.services.deps import CaseState, DetectorDeps, StageEvent
from Detector.services.graph import case_graph, render_mermaid
from Detector.services.ocr import TextractOCR, textract_client

type ProgressCallback = Callable[[StageEvent], Awaitable[None]]
"""Called once per audit event as the run produces it."""


@dataclass(frozen=True, slots=True)
class DetectorPipeline:
    """Runs a presentation through OCR, classification, extraction and reconciliation."""

    deps: DetectorDeps

    @classmethod
    def default(cls, settings: Settings | None = None) -> DetectorPipeline:
        """Build a pipeline on the process-wide settings, prompts and agents, without OCR."""
        return cls(deps=DetectorDeps.default(settings=settings))

    @classmethod
    @asynccontextmanager
    async def open(cls, settings: Settings | None = None) -> AsyncIterator[DetectorPipeline]:
        """A pipeline with OCR, for as long as the block runs.

        The Textract client owns a connection pool, so it is opened once here and shared
        by every case and every branch of every fan-out — not opened per document.

        ```python
        async with DetectorPipeline.open() as pipeline:
            result = await pipeline.run(case)
        ```
        """
        settings = settings or get_settings()
        async with textract_client(settings) as client:
            yield cls(
                deps=DetectorDeps.default(TextractOCR.from_settings(client, settings), settings)
            )

    async def run(self, case: CaseInput) -> CaseResult:
        """Analyse one presentation and return the finished report."""
        return await case_graph.run(
            state=CaseState.for_case(case),
            deps=self.deps,
            inputs=case,
        )

    async def run_with_progress(self, case: CaseInput, on_event: ProgressCallback) -> CaseResult:
        """Analyse one presentation, delivering audit events as they happen.

        Use this behind a websocket: `on_event` fires for each read, classification,
        extraction and reconciliation as it completes, rather than only at the end.

        The events arrive in completion order, not document order, because the documents
        are processed in parallel.
        """
        state = CaseState.for_case(case)
        delivered = 0

        async def drain() -> None:
            """Hand over every event the run has produced since the last call."""
            nonlocal delivered
            while delivered < len(state.events):
                event = state.events[delivered]
                delivered += 1
                await on_event(event)

        async with case_graph.iter(state=state, deps=self.deps, inputs=case) as run:
            async for _ in run:
                await drain()
            await drain()

        result = run.output
        if result is None:  # pragma: no cover - the graph always ends at `reconcile`
            raise RuntimeError(f'case {case.case_id} finished without producing a result')
        return result

    @property
    def ocr_available(self) -> bool:
        """Whether this pipeline can read documents that arrive without text."""
        return self.deps.textract is not None

    @staticmethod
    def diagram(title: str | None = None) -> str:
        """The pipeline as a Mermaid diagram."""
        return render_mermaid(title)


@lru_cache(maxsize=1)
def get_pipeline() -> DetectorPipeline:
    """The process-wide pipeline, for presentations that already carry their text.

    Safe to share across concurrent requests. It has no Textract client, so a document
    with no text fails on its own branch — use `DetectorPipeline.open()` when uploads
    have to be read.
    """
    return DetectorPipeline.default()
