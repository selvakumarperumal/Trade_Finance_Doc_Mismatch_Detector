"""The output of the reconciliation stage: discrepancies and the case verdict."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from Detector.models.documents import ExtractedDocument, TokenUsage
from Detector.models.enums import CaseStatus, DocumentType, Severity


class FieldObservation(BaseModel):
    """One document's version of a field that is under comparison."""

    model_config = ConfigDict(extra='forbid', use_attribute_docstrings=True)

    document_type: DocumentType
    """Which document this value was read from."""

    field: str
    """The field name on that document, e.g. 'beneficiary' or 'shipment_date'."""

    value: str | None = None
    """The value exactly as extracted, or null if the document omits it."""

    document_id: str | None = None
    """The id of the specific document, when more than one of a type was presented."""


class Mismatch(BaseModel):
    """A single discrepancy between two or more presented documents."""

    model_config = ConfigDict(extra='forbid', use_attribute_docstrings=True)

    code: str
    """Short stable slug for this kind of discrepancy, e.g. 'late_shipment'."""

    severity: Severity
    """critical = the bank would refuse; warning = a human should look; info = cosmetic."""

    field: str
    """The business field in dispute, e.g. 'Beneficiary' or 'Port of Discharge'."""

    explanation: str
    """Plain language a bank ops person would understand: what disagrees and why it matters."""

    documents_involved: list[DocumentType] = Field(default_factory=list)
    """Every document family that takes part in this discrepancy."""

    observations: list[FieldObservation] = Field(default_factory=list)
    """The conflicting values, one entry per document."""

    rule_reference: str | None = None
    """The UCP 600 article or ISBP 745 paragraph relied on, when one applies."""

    suggested_action: str | None = None
    """What the beneficiary or the bank would do to cure it."""


class ReconciliationReport(BaseModel):
    """The reconciliation agent's verdict over a full presentation."""

    model_config = ConfigDict(extra='forbid', use_attribute_docstrings=True)

    status: CaseStatus
    """blocked if any critical finding, needs_review if only warnings, else clean."""

    summary: str
    """Two or three sentences an examiner can read before opening the detail."""

    mismatches: list[Mismatch] = Field(default_factory=list)
    """Every discrepancy found, most severe first."""

    matched_fields: list[str] = Field(default_factory=list)
    """Fields that were cross-checked and agreed across documents."""

    missing_documents: list[DocumentType] = Field(default_factory=list)
    """Document families the credit appears to require but that were not presented."""

    @property
    def critical_count(self) -> int:
        """How many findings would cause the bank to refuse."""
        return sum(1 for m in self.mismatches if m.severity is Severity.CRITICAL)

    @property
    def warning_count(self) -> int:
        """How many findings need a human examiner."""
        return sum(1 for m in self.mismatches if m.severity is Severity.WARNING)


class CaseResult(BaseModel):
    """Everything one run of the pipeline produced. This is what the API returns."""

    case_id: str
    status: CaseStatus
    report: ReconciliationReport
    documents: list[ExtractedDocument] = Field(default_factory=list)
    usage: TokenUsage = TokenUsage()
    started_at: datetime
    completed_at: datetime

    @property
    def duration_seconds(self) -> float:
        """Wall clock time the pipeline took."""
        return (self.completed_at - self.started_at).total_seconds()
