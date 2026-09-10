"""Graph state and dependencies.

`DetectorDeps` is everything the graph needs from the outside world and never
mutates. `CaseState` is what one run accumulates as it goes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from pydantic_ai.usage import RunUsage, UsageLimits

from Detector.core.config import Settings, get_settings
from Detector.models.documents import CaseInput, TokenUsage
from Detector.models.enums import DocumentType
from Detector.prompts.registry import get_prompts
from Detector.services.agents import AgentRegistry, build_agents, get_agents
from Detector.services.ocr import TextractOCR


@dataclass(frozen=True, slots=True)
class StageEvent:
    """One thing that happened during a run, for the audit trail and progress push."""

    stage: str
    """Which graph step emitted it, e.g. 'classify' or 'extract'."""

    message: str
    at: datetime
    document_id: str | None = None
    document_type: DocumentType | None = None


@dataclass(slots=True)
class CaseState:
    """Mutable state for one run of the pipeline.

    The fan-out over documents runs as concurrent tasks on a single event loop.
    The mutations below contain no `await`, so they are atomic with respect to
    each other and need no lock.
    """

    case_id: str
    presented_on: date | None = None
    """Copied off the case input so the reconciliation step can date the presentation."""

    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    usage: RunUsage = field(default_factory=RunUsage)
    """Running total across every agent call in the case."""

    events: list[StageEvent] = field(default_factory=list)
    """Ordered audit trail; safe to stream to the client as it grows."""

    @classmethod
    def for_case(cls, case: CaseInput) -> CaseState:
        """Open a fresh run state for one presentation."""
        return cls(case_id=case.case_id, presented_on=case.presented_on)

    def record(
        self,
        stage: str,
        message: str,
        *,
        document_id: str | None = None,
        document_type: DocumentType | None = None,
        usage: RunUsage | None = None,
    ) -> None:
        """Append an audit event and fold in the usage of the call that produced it."""
        if usage is not None:
            self.usage.incr(usage)
        self.events.append(
            StageEvent(
                stage=stage,
                message=message,
                at=datetime.now(UTC),
                document_id=document_id,
                document_type=document_type,
            )
        )

    def usage_snapshot(self) -> TokenUsage:
        """The case's usage so far, in storable form."""
        return TokenUsage.from_run_usage(self.usage)


@dataclass(frozen=True, slots=True)
class DetectorDeps:
    """Immutable dependencies handed to every step in the graph."""

    agents: AgentRegistry
    settings: Settings
    usage_limits: UsageLimits | None = None
    """Applied per agent run, not per case."""

    textract: TextractOCR | None = None
    """The OCR client, shared by every branch of the fan-out.

    `None` means documents must arrive with their text already extracted; the OCR step
    then fails those documents individually instead of the whole case. It is not built
    here because opening the client needs an `await` — see `DetectorPipeline.open`.
    """

    @classmethod
    def default(
        cls,
        textract: TextractOCR | None = None,
        settings: Settings | None = None,
    ) -> DetectorDeps:
        """Build deps from the process-wide settings, prompts and agents.

        Passing `settings` explicitly builds a fresh agent registry from it, rather than
        reusing the cached process-wide one — otherwise the argument would be a lie, and
        a test or a second deployment shape could not change the models.
        """
        if settings is None:
            settings, agents = get_settings(), get_agents()
        else:
            agents = build_agents(settings, get_prompts())
        return cls(
            agents=agents,
            settings=settings,
            usage_limits=build_usage_limits(settings),
            textract=textract,
        )


def build_usage_limits(settings: Settings) -> UsageLimits:
    """Translate the configured budgets into Pydantic AI usage limits."""
    return UsageLimits(
        request_limit=settings.request_limit,
        total_tokens_limit=settings.total_tokens_limit,
        cost_limit=settings.cost_limit,
    )
