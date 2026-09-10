"""A submitted case: where it has got to, and what it produced.

Analysing a presentation takes minutes, so submitting one and collecting its answer are
two different acts. This is what sits between them — and it is the only thing the API
hands back, whether you poll it or watch it over a websocket.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from Detector.models.enums import DocumentType, JobStatus
from Detector.models.reconciliation import CaseResult


class CaseEvent(BaseModel):
    """One thing that happened during a run, as the caller sees it."""

    model_config = ConfigDict(frozen=True)

    stage: str
    """Which graph step emitted it: 'ingest', 'ocr', 'classify', 'extract', 'reconcile'."""

    message: str
    at: datetime
    document_id: str | None = None
    document_type: DocumentType | None = None


class CaseRecord(BaseModel):
    """Everything known about one submitted case."""

    model_config = ConfigDict(use_attribute_docstrings=True)

    case_id: str
    status: JobStatus = JobStatus.QUEUED

    document_count: int = 0
    """How many documents were submitted, known before any of them are read."""

    submitted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None

    events: list[CaseEvent] = Field(default_factory=list)
    """The audit trail so far, in the order the run produced it."""

    result: CaseResult | None = None
    """The finished report. Present only once `status` is `succeeded`."""

    error: str | None = None
    """Why the run failed. Present only once `status` is `failed`."""

    error_code: str | None = None
    """Stable slug for the failure, so a caller branches on this rather than the text."""

    @property
    def is_terminal(self) -> bool:
        """Whether this case will not change again."""
        return self.status.is_terminal
