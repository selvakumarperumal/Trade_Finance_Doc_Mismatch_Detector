"""Documents as they travel through the pipeline: raw -> classified -> extracted."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

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
    """One uploaded document after text extraction, before the model sees it."""

    document_id: str
    """Stable identifier assigned by the caller (upload id, row id, ...)."""

    text: str
    """Plain text pulled out of the PDF/image. This is the only evidence the agents get."""

    filename: str | None = None
    page_count: int | None = None
    declared_type: DocumentType | None = None
    """Type asserted by the uploader, if any. Used as a hint, never as the truth."""


class CaseInput(BaseModel):
    """The unit of work: every document presented under one credit."""

    case_id: str
    documents: list[RawDocument] = Field(default_factory=list)
    presented_on: date | None = None
    """The date the documents were presented to the bank, when known. Required for
    the UCP 600 Art 14(c) presentation-period check."""

    notes: str | None = None
    """Free-text context from the ops user, passed to the reconciliation stage."""


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

    The subclasses below carry no extra data; they exist so the graph's
    `Decision` node can dispatch on type instead of on a string comparison,
    which makes the routing table exhaustively checkable by a type checker.
    """

    raw: RawDocument
    classification: Classification
    usage: TokenUsage = TokenUsage()
    """What classifying this document cost, carried forward so the per-document
    total in `ExtractedDocument` covers the whole branch, not just extraction."""


@dataclass(frozen=True, slots=True)
class LetterOfCreditDoc(RoutedDocument):
    """Routed to the LC extractor."""


@dataclass(frozen=True, slots=True)
class CommercialInvoiceDoc(RoutedDocument):
    """Routed to the invoice extractor."""


@dataclass(frozen=True, slots=True)
class BillOfLadingDoc(RoutedDocument):
    """Routed to the bill of lading extractor."""


@dataclass(frozen=True, slots=True)
class PackingListDoc(RoutedDocument):
    """Routed to the packing list extractor."""


@dataclass(frozen=True, slots=True)
class CertificateOfOriginDoc(RoutedDocument):
    """Routed to the certificate of origin extractor."""


@dataclass(frozen=True, slots=True)
class InsuranceCertificateDoc(RoutedDocument):
    """Routed to the insurance certificate extractor."""


@dataclass(frozen=True, slots=True)
class BillOfExchangeDoc(RoutedDocument):
    """Routed to the bill of exchange extractor."""


@dataclass(frozen=True, slots=True)
class InspectionCertificateDoc(RoutedDocument):
    """Routed to the inspection certificate extractor."""


@dataclass(frozen=True, slots=True)
class UnclassifiedDoc(RoutedDocument):
    """Not recognised as any known family; skips extraction."""


type RoutedDocuments = (
    LetterOfCreditDoc
    | CommercialInvoiceDoc
    | BillOfLadingDoc
    | PackingListDoc
    | CertificateOfOriginDoc
    | InsuranceCertificateDoc
    | BillOfExchangeDoc
    | InspectionCertificateDoc
    | UnclassifiedDoc
)
"""Every branch the routing decision must handle."""


ROUTED_DOCUMENT_TYPES: dict[DocumentType, type[RoutedDocument]] = {
    DocumentType.LETTER_OF_CREDIT: LetterOfCreditDoc,
    DocumentType.COMMERCIAL_INVOICE: CommercialInvoiceDoc,
    DocumentType.BILL_OF_LADING: BillOfLadingDoc,
    DocumentType.PACKING_LIST: PackingListDoc,
    DocumentType.CERTIFICATE_OF_ORIGIN: CertificateOfOriginDoc,
    DocumentType.INSURANCE_CERTIFICATE: InsuranceCertificateDoc,
    DocumentType.BILL_OF_EXCHANGE: BillOfExchangeDoc,
    DocumentType.INSPECTION_CERTIFICATE: InspectionCertificateDoc,
    DocumentType.UNKNOWN: UnclassifiedDoc,
}
"""Maps a classifier verdict onto the envelope that routes it."""


class ExtractedDocument(BaseModel):
    """The output of one document's classify-then-extract branch."""

    document_id: str
    document_type: DocumentType
    confidence: float
    classification_reasoning: str
    filename: str | None = None
    payload: ExtractionPayload | None = None
    """`None` when the document was unclassifiable or extraction failed."""

    error: str | None = None
    """Why `payload` is `None`, when it is."""

    usage: TokenUsage = TokenUsage()

    @property
    def is_usable(self) -> bool:
        """Whether this document can contribute evidence to reconciliation."""
        return self.payload is not None
