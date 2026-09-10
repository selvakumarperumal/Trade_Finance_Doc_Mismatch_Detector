"""Documents as they travel through the pipeline: raw -> classified -> extracted."""

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
    """A serialisable snapshot of what a stage cost.

    `pydantic_ai.usage.RunUsage` is the live accumulator; this is the flattened
    form that survives a trip through the database and back.
    """

    model_config = ConfigDict(frozen=True)

    requests: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @classmethod
    def from_run_usage(cls, usage: RunUsage) -> TokenUsage:
        """Flatten a Pydantic AI `RunUsage` into a stored snapshot."""
        return cls(
            requests=usage.requests,
            tool_calls=usage.tool_calls,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
        )

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            requests=self.requests + other.requests,
            tool_calls=self.tool_calls + other.tool_calls,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )


class RawDocument(BaseModel):
    """One uploaded document on its way to the agents.

    It arrives either with `text` already extracted, or with the file itself in
    `content` for the OCR step to read with Textract. The bytes are never stored:
    they are read once and dropped, so every step after OCR sees one shape whichever
    way the document came in.
    """

    document_id: str
    """Stable identifier assigned by the caller (upload id, row id, ...)."""

    text: str = ''
    """Plain text of the document — the only evidence the agents get. Empty means the
    document has not been read yet."""

    content: bytes | None = Field(default=None, repr=False, exclude=True)
    """The file itself, for OCR. Dropped as soon as the OCR step has read it.

    Excluded from serialisation: a presentation is megabytes of scanned paper, and
    none of it belongs in an API response, a log line or a stored case record.
    """

    filename: str | None = None

    page_count: int | None = None
    """Pages the document turned out to have, filled in once it has been read."""

    ocr_error: str | None = None
    """Why `text` is still empty after the OCR step, when it is."""

    @property
    def needs_ocr(self) -> bool:
        """Whether this document still has to be read before a model can see it."""
        return not self.text.strip()


class CaseInput(BaseModel):
    """The unit of work: every document presented under one credit."""

    case_id: str
    documents: list[RawDocument] = Field(default_factory=list)
    presented_on: date | None = None
    """The date the documents were presented to the bank, when known. Required for
    the UCP 600 Art 14(c) presentation-period check."""

    @model_validator(mode='after')
    def _document_ids_are_unique(self) -> CaseInput:
        """Reject a case that presents two documents under the same id.

        Every later stage keys on `document_id`: the audit trail, the per-document
        result, and the observations the reconciler cites in a finding. Two documents
        sharing an id makes a finding point at the wrong piece of paper, which is worse
        than refusing the case.
        """
        counts = Counter(document.document_id for document in self.documents)
        duplicates = sorted(name for name, count in counts.items() if count > 1)
        if duplicates:
            raise ValueError(f'duplicate document ids in case {self.case_id}: {duplicates}')
        return self

    @property
    def total_bytes(self) -> int:
        """How much file content this case is carrying, for the memory ceiling."""
        return sum(len(d.content) for d in self.documents if d.content)


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
    `ExtractedDocument` covers the whole branch, not just extraction."""

    @property
    def document_type(self) -> DocumentType:
        """The family this document was routed to."""
        return self.classification.document_type


class ExtractedDocument(BaseModel):
    """The output of one document's classify-then-extract branch."""

    document_id: str
    document_type: DocumentType
    confidence: float
    classification_reasoning: str
    filename: str | None = None

    page_count: int | None = None
    """How many pages were read, for a caller showing what it processed."""

    payload: ExtractionPayload | None = None
    """`None` when the document was unclassifiable or extraction failed."""

    error: str | None = None
    """Why `payload` is `None`, when it is."""

    usage: TokenUsage = TokenUsage()

    @property
    def is_usable(self) -> bool:
        """Whether this document can contribute evidence to reconciliation."""
        return self.payload is not None
