"""Documents as they travel through the pipeline: raw -> classified -> extracted.

Three shapes, one per stage, and each carries forward what the next one needs:

    RawDocument       what was uploaded, plus its text once OCR has read it
    RoutedDocument    the same document, now with the classifier's verdict
    ExtractedDocument the finished branch: typed fields, or the reason there are none
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, model_validator

from Detector.models.enums import DocumentType
from Detector.models.extractions import ExtractionPayload

if TYPE_CHECKING:
    from pydantic_ai.usage import RunUsage


class TokenUsage(BaseModel):
    """What a stage cost, in the four numbers the case result reports.

    `pydantic_ai.usage.RunUsage` is the live accumulator; this is the flattened form that
    survives a trip through JSON and back.
    """

    model_config = ConfigDict(frozen=True)

    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    @classmethod
    def from_run_usage(cls, usage: RunUsage) -> TokenUsage:
        """Flatten a Pydantic AI `RunUsage` into a stored snapshot."""
        return cls(
            requests=usage.requests,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
        )

    def __add__(self, other: TokenUsage) -> TokenUsage:
        """Add two snapshots, so a case can total what its branches each spent."""
        return TokenUsage(
            requests=self.requests + other.requests,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
        )


class RawDocument(BaseModel):
    """One uploaded document on its way to the agents.

    It arrives either with `text` already extracted, or with the file itself in `content`
    for the OCR step to read. Every step after OCR sees the same shape whichever way the
    document came in.
    """

    document_id: str
    """What identifies this document everywhere downstream — in the audit trail, in the
    per-document result, and in the observations a finding cites. `CaseInput` refuses a
    presentation where two documents share one."""

    text: str = ''
    """The document's text, and the only evidence the agents ever get. Empty means it has
    not been read yet."""

    content: bytes | None = Field(default=None, repr=False, exclude=True)
    """The file itself, for OCR. Dropped as soon as the OCR step has read it, and
    `exclude=True` keeps megabytes of scanned paper out of every API response, log line
    and stored case record."""

    filename: str | None = None
    """What the uploader called it, shown back to the caller. Never trusted to say what
    format the file is — that comes from the bytes."""

    page_count: int | None = None
    """Pages the document turned out to have, filled in by the OCR step. Textract bills
    per page, so this is what a caller checks to see what a case actually cost."""

    ocr_error: str | None = None
    """Why `text` is still empty after the OCR step, when it is. A document that could
    not be read becomes a finding rather than a failed case."""

    @property
    def needs_ocr(self) -> bool:
        """Whether this document still has to be read before a model can see it."""
        return not self.text.strip()


class CaseInput(BaseModel):
    """The unit of work: every document presented under one credit."""

    case_id: str
    documents: list[RawDocument] = Field(default_factory=list)

    presented_on: date | None = None
    """The date the documents reached the bank. UCP 600 Art 14(c) gives a beneficiary a
    limited window to present after shipment, so without this date that one check is
    skipped rather than guessed at. Every other check runs either way."""

    @model_validator(mode='after')
    def _document_ids_are_unique(self) -> CaseInput:
        """Reject a case that presents two documents under the same id.

        Every later stage keys on `document_id`, so two documents sharing one makes a
        finding point at the wrong piece of paper — worse than refusing the case.
        """
        counts = Counter(document.document_id for document in self.documents)
        duplicates = sorted(name for name, count in counts.items() if count > 1)
        if duplicates:
            raise ValueError(f'duplicate document ids in case {self.case_id}: {duplicates}')
        return self


class Classification(BaseModel):
    """What the classifier agent decided about a single document."""

    model_config = ConfigDict(extra='forbid', use_attribute_docstrings=True)

    document_type: DocumentType
    """The family this document belongs to, or `unknown` if it doesn't clearly match one."""

    confidence: float = Field(ge=0.0, le=1.0)
    """How sure the classifier is, from 0 to 1."""

    reasoning: str
    """The markers in the text that drove the decision, in one or two sentences."""


@dataclass(frozen=True, slots=True)
class RoutedDocument:
    """A classified document on its way to an extractor.

    Which extractor is decided by `classification.document_type`; the graph builds one
    branch per family from the same table it builds the extraction steps from, so there
    is nothing here to keep in step with it.
    """

    raw: RawDocument
    classification: Classification

    usage: TokenUsage = field(default_factory=TokenUsage)
    """What classifying this document cost, carried forward so the per-document total in
    `ExtractedDocument` covers the whole branch rather than extraction alone."""

    @property
    def document_type(self) -> DocumentType:
        """The family this document was routed to."""
        return self.classification.document_type


class ExtractedDocument(BaseModel):
    """The output of one document's classify-then-extract branch.

    Always produced, even when nothing could be read: a document that failed carries its
    reason in `error`, so the case still returns a result for every document submitted.
    """

    document_id: str
    """The id it was submitted under, so a caller can match this back to its upload."""

    document_type: DocumentType
    confidence: float
    classification_reasoning: str
    """The classifier's three fields, flattened — an examiner reviewing a finding wants
    to see what the document was taken to be and how sure that was."""

    filename: str | None = None
    page_count: int | None = None

    payload: ExtractionPayload | None = None
    """The typed fields, under the schema for this document's family. `None` when the
    document was unclassifiable or extraction failed."""

    error: str | None = None
    """Why `payload` is `None`, when it is."""

    usage: TokenUsage = TokenUsage()

    @property
    def is_usable(self) -> bool:
        """Whether this document can contribute evidence to reconciliation."""
        return self.payload is not None
