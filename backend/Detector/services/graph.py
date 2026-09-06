"""The Pydantic Graph pipeline.

    start -> ingest -> (fan out per document)
                          classify -> route by document type -> extract_<family>
                       (join back)
                    -> reconcile -> end

The fan-out is a `map` fork, so every document is classified and extracted
concurrently; the `collect` join waits for all of them and hands the
reconciliation step the full set. Routing is a `Decision` that dispatches on the
envelope type the classifier produced, which keeps the branch table
exhaustively checkable rather than a chain of string comparisons.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast

from pydantic_ai.exceptions import AgentRunError, RunCancelled, UsageLimitExceeded
from pydantic_ai.format_prompt import format_as_xml
from pydantic_graph import (
    Decision,
    Graph,
    GraphBuilder,
    Step,
    StepContext,
    reduce_list_append,
)

from Detector.models.documents import (
    ROUTED_DOCUMENT_TYPES,
    BillOfExchangeDoc,
    BillOfLadingDoc,
    CaseInput,
    CertificateOfOriginDoc,
    Classification,
    CommercialInvoiceDoc,
    ExtractedDocument,
    InspectionCertificateDoc,
    InsuranceCertificateDoc,
    LetterOfCreditDoc,
    PackingListDoc,
    RawDocument,
    RoutedDocument,
    RoutedDocuments,
    TokenUsage,
    UnclassifiedDoc,
)
from Detector.models.enums import CaseStatus, DocumentType, Severity
from Detector.models.reconciliation import CaseResult, Mismatch, ReconciliationReport
from Detector.services.deps import CaseState, DetectorDeps

COLLECT_ID = "collect_extractions"
"""Node id of the join. Named so the fan-out can point at it for the empty-case path."""

FAN_OUT_ID = "fan_out_documents"
"""Node id of the map fork over the case's documents."""

_SEVERITY_ORDER = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}

builder = GraphBuilder(
    name="trade_finance_doc_mismatch",
    state_type=CaseState,
    deps_type=DetectorDeps,
    input_type=CaseInput,
    output_type=CaseResult,
)

collect = builder.join(
    reduce_list_append,
    initial_factory=list[ExtractedDocument],
    node_id=COLLECT_ID,
)
"""Gathers one `ExtractedDocument` per presented document, in completion order."""


# --- prompt construction -----------------------------------------------------


def _document_prompt(raw: RawDocument) -> str:
    """Wrap one document's text so the model can see its identity and its boundaries."""
    hint = (
        f"\nThe uploader labelled this as {raw.declared_type.value}; treat that as a hint only."
        if raw.declared_type is not None
        else ""
    )
    return (
        f"Document id: {raw.document_id}\n"
        f'Filename: {raw.filename or "unknown"}\n'
        f'Pages: {raw.page_count if raw.page_count is not None else "unknown"}{hint}\n\n'
        f"<document_text>\n{raw.text}\n</document_text>"
    )


def _reconciliation_prompt(
    state: CaseState, documents: Sequence[ExtractedDocument]
) -> str:
    """Render the extracted documents as the evidence for the compliance check."""
    evidence = [
        {
            "document_id": document.document_id,
            "document_type": document.document_type.value,
            "classification_confidence": round(document.confidence, 2),
            "fields": document.payload,
        }
        for document in documents
    ]
    parts = [
        f"Case: {state.case_id}",
        f"Documents presented: {len(documents)}",
    ]
    if state.presented_on is not None:
        parts.append(f"Date of presentation: {state.presented_on.isoformat()}")
    else:
        parts.append(
            "Date of presentation: not supplied. Do not raise a presentation-period "
            "finding on a date you had to assume."
        )
    if state.notes:
        parts.append(f"Operator notes: {state.notes}")
    parts.append(
        "\nUse the deterministic tools for every date and amount comparison rather than "
        "computing them yourself, and quote the figures they return in your findings.\n"
    )
    parts.append(
        format_as_xml(evidence, root_tag="extracted_documents", item_tag="document")
    )
    return "\n".join(parts)


# --- steps -------------------------------------------------------------------


@builder.step
async def ingest(
    ctx: StepContext[CaseState, DetectorDeps, CaseInput],
) -> list[RawDocument]:
    """Validate the presentation and hand its documents to the fan-out.

    Oversized or oversubscribed cases fail here rather than being silently
    truncated, because a truncated document produces a confident wrong answer.
    """
    case = ctx.inputs
    settings = ctx.deps.settings

    if len(case.documents) > settings.max_documents_per_case:
        raise ValueError(
            f"case {case.case_id} has {len(case.documents)} documents, "
            f"above the limit of {settings.max_documents_per_case}"
        )

    for document in case.documents:
        if len(document.text) > settings.max_document_chars:
            raise ValueError(
                f"document {document.document_id} has {len(document.text)} characters, "
                f"above the limit of {settings.max_document_chars}; split it before submitting"
            )
        if not document.text.strip():
            raise ValueError(f"document {document.document_id} has no extracted text")

    ctx.state.record("ingest", f"accepted {len(case.documents)} document(s)")
    return case.documents


@builder.step
async def classify(
    ctx: StepContext[CaseState, DetectorDeps, RawDocument],
) -> RoutedDocuments:
    """Decide which document family this text belongs to, and wrap it for routing.

    A failure here degrades one document to unclassified instead of failing the
    case: the remaining documents still get reconciled, and the gap shows up as a
    finding rather than a 500.
    """
    raw = ctx.inputs
    deps = ctx.deps

    try:
        result = await deps.agents.classifier.run(
            _document_prompt(raw),
            usage_limits=deps.usage_limits,
        )
    except (UsageLimitExceeded, RunCancelled):
        raise
    except AgentRunError as exc:
        ctx.state.record(
            "classify",
            f"classification failed: {exc}",
            document_id=raw.document_id,
            document_type=DocumentType.UNKNOWN,
        )
        return UnclassifiedDoc(
            raw=raw,
            classification=Classification(
                document_type=DocumentType.UNKNOWN,
                confidence=0.0,
                reasoning=f"classification failed: {exc}",
            ),
        )

    usage = TokenUsage.from_run_usage(result.usage)

    classification = result.output
    threshold = deps.settings.min_classification_confidence
    if classification.confidence < threshold:
        ctx.state.record(
            "classify",
            f"classified as {classification.document_type.value} at "
            f"{classification.confidence:.2f}, below the {threshold:.2f} threshold; "
            "treating as unclassified",
            document_id=raw.document_id,
            document_type=DocumentType.UNKNOWN,
            usage=result.usage,
        )
        return UnclassifiedDoc(raw=raw, classification=classification, usage=usage)

    ctx.state.record(
        "classify",
        f"classified as {classification.document_type.value} at {classification.confidence:.2f}",
        document_id=raw.document_id,
        document_type=classification.document_type,
        usage=result.usage,
    )
    envelope = ROUTED_DOCUMENT_TYPES[classification.document_type]
    return cast(
        RoutedDocuments, envelope(raw=raw, classification=classification, usage=usage)
    )


async def _extract(
    ctx: StepContext[CaseState, DetectorDeps, RoutedDocument],
    document_type: DocumentType,
) -> ExtractedDocument:
    """Run one family's extraction agent over one document."""
    routed = ctx.inputs
    raw = routed.raw
    deps = ctx.deps

    base = {
        "document_id": raw.document_id,
        "filename": raw.filename,
        "document_type": document_type,
        "confidence": routed.classification.confidence,
        "classification_reasoning": routed.classification.reasoning,
    }

    try:
        result = await deps.agents.extractor(document_type).run(
            _document_prompt(raw),
            usage_limits=deps.usage_limits,
        )
    except (UsageLimitExceeded, RunCancelled):
        raise
    except AgentRunError as exc:
        ctx.state.record(
            "extract",
            f"extraction failed: {exc}",
            document_id=raw.document_id,
            document_type=document_type,
        )
        return ExtractedDocument(
            **base,
            payload=None,
            error=f"extraction failed: {exc}",
            usage=routed.usage,
        )

    usage = result.usage
    ctx.state.record(
        "extract",
        f"extracted {document_type.value}",
        document_id=raw.document_id,
        document_type=document_type,
        usage=usage,
    )
    return ExtractedDocument(
        **base,
        payload=result.output,
        usage=routed.usage + TokenUsage.from_run_usage(usage),
    )


def _extraction_step(
    document_type: DocumentType,
) -> Step[CaseState, DetectorDeps, Any, ExtractedDocument]:
    """Build the graph step that extracts one document family.

    The eight families differ only in their agent and their payload type, so the
    step bodies are generated rather than written out eight times. Each still
    gets its own node id, which is what shows up in the rendered graph and in the
    instrumentation spans.
    """

    async def extract(
        ctx: StepContext[CaseState, DetectorDeps, RoutedDocument],
    ) -> ExtractedDocument:
        return await _extract(ctx, document_type)

    return builder.step(
        extract,
        node_id=f"extract_{document_type.value}",
        label=document_type.value.replace("_", " "),
    )


extract_letter_of_credit = _extraction_step(DocumentType.LETTER_OF_CREDIT)
extract_commercial_invoice = _extraction_step(DocumentType.COMMERCIAL_INVOICE)
extract_bill_of_lading = _extraction_step(DocumentType.BILL_OF_LADING)
extract_packing_list = _extraction_step(DocumentType.PACKING_LIST)
extract_certificate_of_origin = _extraction_step(DocumentType.CERTIFICATE_OF_ORIGIN)
extract_insurance_certificate = _extraction_step(DocumentType.INSURANCE_CERTIFICATE)
extract_bill_of_exchange = _extraction_step(DocumentType.BILL_OF_EXCHANGE)
extract_inspection_certificate = _extraction_step(DocumentType.INSPECTION_CERTIFICATE)


@builder.step(node_id="skip_unclassified", label="unknown")
async def skip_unclassified(
    ctx: StepContext[CaseState, DetectorDeps, UnclassifiedDoc],
) -> ExtractedDocument:
    """Carry an unrecognised document through to the join without extracting it.

    It still reaches reconciliation so the examiner sees that something was
    presented that the pipeline could not read.
    """
    routed = ctx.inputs
    ctx.state.record(
        "skip",
        "not recognised as a known document family",
        document_id=routed.raw.document_id,
        document_type=DocumentType.UNKNOWN,
    )
    return ExtractedDocument(
        document_id=routed.raw.document_id,
        filename=routed.raw.filename,
        document_type=DocumentType.UNKNOWN,
        confidence=routed.classification.confidence,
        classification_reasoning=routed.classification.reasoning,
        payload=None,
        error="document type could not be determined; not extracted",
        usage=routed.usage,
    )


@builder.step
async def reconcile(
    ctx: StepContext[CaseState, DetectorDeps, list[ExtractedDocument]],
) -> CaseResult:
    """Cross-check the extracted documents and assemble the case result."""
    state = ctx.state
    documents = sorted(ctx.inputs, key=lambda document: document.document_id)
    usable = [document for document in documents if document.is_usable]

    if not usable:
        report = _no_evidence_report(documents)
        state.record("reconcile", "no usable extractions; skipped the compliance check")
    else:
        result = await ctx.deps.agents.reconciler.run(
            _reconciliation_prompt(state, usable),
            usage_limits=ctx.deps.usage_limits,
        )
        report = _finalise_report(result.output, documents)
        state.record(
            "reconcile",
            f"{report.status.value}: {report.critical_count} critical, {report.warning_count} warning",
            usage=result.usage,
        )

    return CaseResult(
        case_id=state.case_id,
        status=report.status,
        report=report,
        documents=documents,
        usage=state.usage_snapshot(),
        started_at=state.started_at,
        completed_at=datetime.now(UTC),
    )


# --- report post-processing --------------------------------------------------


def _derive_status(mismatches: Sequence[Mismatch]) -> CaseStatus:
    """Map findings to a verdict.

    The prompt asks the model for this too, but the mapping is a rule, not a
    judgement, so the rule wins: a report with a critical finding is blocked
    whatever the model wrote in `status`.
    """
    severities = {mismatch.severity for mismatch in mismatches}
    if Severity.CRITICAL in severities:
        return CaseStatus.BLOCKED
    if Severity.WARNING in severities:
        return CaseStatus.NEEDS_REVIEW
    return CaseStatus.CLEAN


def _finalise_report(
    report: ReconciliationReport,
    documents: Sequence[ExtractedDocument],
) -> ReconciliationReport:
    """Sort the findings, enforce the status rule, and flag unreadable documents."""
    mismatches = sorted(report.mismatches, key=lambda m: _SEVERITY_ORDER[m.severity])
    unreadable = [document for document in documents if not document.is_usable]
    if unreadable:
        mismatches.append(
            Mismatch(
                code="document_not_extracted",
                severity=Severity.WARNING,
                field="document set",
                explanation=(
                    f"{len(unreadable)} presented document(s) could not be read into structured "
                    "fields and took no part in the cross-checks: "
                    + ", ".join(f"{d.document_id} ({d.error})" for d in unreadable)
                ),
                documents_involved=[DocumentType.UNKNOWN],
                suggested_action="Re-upload a clearer copy, or examine these documents manually.",
            )
        )
        mismatches.sort(key=lambda m: _SEVERITY_ORDER[m.severity])

    return report.model_copy(
        update={"mismatches": mismatches, "status": _derive_status(mismatches)}
    )


def _no_evidence_report(documents: Sequence[ExtractedDocument]) -> ReconciliationReport:
    """The verdict when nothing could be extracted, so there is nothing to compare."""
    detail = (
        "No documents were presented."
        if not documents
        else f"None of the {len(documents)} presented document(s) could be read into structured fields."
    )
    return ReconciliationReport(
        status=CaseStatus.NEEDS_REVIEW,
        summary=f"{detail} No cross-document checks were performed.",
        mismatches=[
            Mismatch(
                code="no_usable_documents",
                severity=Severity.WARNING,
                field="document set",
                explanation=detail,
                documents_involved=[DocumentType.UNKNOWN],
                suggested_action="Check the uploads and the text extraction step, then resubmit.",
            )
        ],
    )


# --- wiring ------------------------------------------------------------------


def _routing_decision() -> Decision[CaseState, DetectorDeps, RoutedDocuments]:
    """The branch table from a classified document to its extractor.

    Branches match on the envelope class, so adding a document family without
    adding a branch here is a type error rather than a silent fall-through.
    """
    return (
        builder.decision(
            node_id="route_by_document_type", note="UCP 600 document families"
        )
        .branch(builder.match(LetterOfCreditDoc).to(extract_letter_of_credit))
        .branch(builder.match(CommercialInvoiceDoc).to(extract_commercial_invoice))
        .branch(builder.match(BillOfLadingDoc).to(extract_bill_of_lading))
        .branch(builder.match(PackingListDoc).to(extract_packing_list))
        .branch(builder.match(CertificateOfOriginDoc).to(extract_certificate_of_origin))
        .branch(
            builder.match(InsuranceCertificateDoc).to(extract_insurance_certificate)
        )
        .branch(builder.match(BillOfExchangeDoc).to(extract_bill_of_exchange))
        .branch(
            builder.match(InspectionCertificateDoc).to(extract_inspection_certificate)
        )
        .branch(builder.match(UnclassifiedDoc).to(skip_unclassified))
    )


EXTRACTION_STEPS: tuple[Step[CaseState, DetectorDeps, Any, ExtractedDocument], ...] = (
    extract_letter_of_credit,
    extract_commercial_invoice,
    extract_bill_of_lading,
    extract_packing_list,
    extract_certificate_of_origin,
    extract_insurance_certificate,
    extract_bill_of_exchange,
    extract_inspection_certificate,
    skip_unclassified,
)
"""Everything that can feed the join. Ordering only affects the rendered diagram."""


builder.add(
    builder.edge_from(builder.start_node).to(ingest),
    builder.edge_from(ingest)
    .label("per document")
    .map(fork_id=FAN_OUT_ID, downstream_join_id=COLLECT_ID)
    .to(classify),
    builder.edge_from(classify).to(_routing_decision()),
    builder.edge_from(*EXTRACTION_STEPS).to(collect),
    builder.edge_from(collect).label("all documents").to(reconcile),
    builder.edge_from(reconcile).to(builder.end_node),
)

case_graph: Graph[CaseState, DetectorDeps, CaseInput, CaseResult] = builder.build()
"""The built graph. Stateless and safe to share across concurrent runs."""


def render_mermaid(title: str | None = None) -> str:
    """The pipeline as a Mermaid diagram, for docs and for debugging the wiring."""
    return case_graph.render(title=title)
