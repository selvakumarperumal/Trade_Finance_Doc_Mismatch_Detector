"""The entry point the API layer calls.

Nothing above this module needs to know about Pydantic AI or Pydantic Graph:
hand it a `CaseInput`, get back a `CaseResult`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache

from Detector.core.observability import Span, configure_observability, span
from Detector.models.documents import CaseInput
from Detector.models.reconciliation import CaseResult
from Detector.services.deps import CaseState, DetectorDeps, StageEvent
from Detector.services.graph import case_graph, render_mermaid

type ProgressCallback = Callable[[StageEvent], Awaitable[None]]
"""Called once per audit event as the run produces it."""


@dataclass(frozen=True, slots=True)
class DetectorPipeline:
    """Runs a presentation through classification, extraction and reconciliation."""

    deps: DetectorDeps

    @classmethod
    def default(cls) -> DetectorPipeline:
        """Build a pipeline on the process-wide settings, prompts and agents."""
        return cls(deps=DetectorDeps.default())

    async def run(self, case: CaseInput) -> CaseResult:
        """Analyse one presentation and return the finished report."""
        with span(
            'analyse case {case_id}',
            case_id=case.case_id,
            document_count=len(case.documents),
        ) as case_span:
            result = await case_graph.run(
                state=CaseState.for_case(case),
                deps=self.deps,
                inputs=case,
            )
            _annotate(case_span, result)
            return result

    async def run_with_progress(self, case: CaseInput, on_event: ProgressCallback) -> CaseResult:
        """Analyse one presentation, delivering audit events as they happen.

        Use this behind a websocket: `on_event` fires for each classification,
        extraction and reconciliation as it completes, rather than only at the end.

        The events arrive in completion order, not document order, because the
        documents are processed in parallel.
        """
        state = CaseState.for_case(case)
        delivered = 0

        async def drain() -> None:
            nonlocal delivered
            while delivered < len(state.events):
                event = state.events[delivered]
                delivered += 1
                await on_event(event)

        with span(
            'analyse case {case_id}',
            case_id=case.case_id,
            document_count=len(case.documents),
            streaming=True,
        ) as case_span:
            async with case_graph.iter(state=state, deps=self.deps, inputs=case) as run:
                async for _ in run:
                    await drain()
                await drain()

            result = run.output
            if result is None:  # pragma: no cover - the graph always ends at `reconcile`
                raise RuntimeError(f'case {case.case_id} finished without producing a result')
            _annotate(case_span, result)
            return result

    @staticmethod
    def diagram(title: str | None = None) -> str:
        """The pipeline as a Mermaid diagram."""
        return render_mermaid(title)


def _annotate(case_span: Span, result: CaseResult) -> None:
    """Put the verdict on the case span, so a trace can be searched by outcome.

    These are the fields worth querying in Logfire: 'show me every blocked case
    last week', 'which cases cost the most tokens', 'how many documents failed
    to extract'.
    """
    case_span.set_attributes(
        {
            'case.status': result.status.value,
            'case.critical_findings': result.report.critical_count,
            'case.warning_findings': result.report.warning_count,
            'case.documents_extracted': sum(1 for d in result.documents if d.is_usable),
            'case.documents_failed': sum(1 for d in result.documents if not d.is_usable),
            'case.duration_seconds': result.duration_seconds,
            'case.model_requests': result.usage.requests,
            'case.input_tokens': result.usage.input_tokens,
            'case.output_tokens': result.usage.output_tokens,
            'case.cache_read_tokens': result.usage.cache_read_tokens,
        }
    )


@lru_cache(maxsize=1)
def get_pipeline() -> DetectorPipeline:
    """The process-wide pipeline. Safe to share across concurrent requests.

    Configures Logfire on first use as a fallback. Prefer calling
    `configure_observability()` yourself at startup, before anything else runs.
    """
    configure_observability()
    return DetectorPipeline.default()
