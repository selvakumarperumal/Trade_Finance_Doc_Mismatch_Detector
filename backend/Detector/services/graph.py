"""The Pydantic Graph pipeline.

    start -> ingest -> (fan out per document)
                          ocr -> classify -> route by document type -> extract_<family>
                       (join back)
                    -> reconcile -> end

The fan-out is a `map` fork, so every document is read, classified and extracted
concurrently; the `collect` join waits for all of them and hands the
reconciliation step the full set. Routing is a `Decision` that dispatches on the
envelope type the classifier produced, which keeps the branch table
exhaustively checkable rather than a chain of string comparisons.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from pydantic_ai.exceptions import RunCancelled, UsageLimitExceeded
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
    CaseInput,
    Classification,
    ExtractedDocument,
    RawDocument,
    RoutedDocument,
    TokenUsage,
)
from Detector.models.enums import CaseStatus, DocumentType, Severity
from Detector.models.extractions import EXTRACTION_PAYLOAD_TYPES
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
    """Wrap one document's text so the model can see its identity and its boundaries.

    The uploader asserts nothing about what a document is: they upload a file and
    the classifier decides. So the only evidence here is the text itself, fenced
    off in `<document_text>` tags, plus enough identity for the model to refer to
    the document in its reasoning.
    """
    return (
        f"Document id: {raw.document_id}\n"
        f'Filename: {raw.filename or "unknown"}\n'
        f'Pages: {raw.page_count if raw.page_count is not None else "unknown"}\n\n'
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
        if document.needs_ocr:
            if not document.content:
                raise ValueError(
                    f"document {document.document_id} has no text and no bytes to OCR; "
                    "give it `text` or `content`"
                )
            if len(document.content) > settings.max_document_bytes:
                raise ValueError(
                    f"document {document.document_id} is {len(document.content)} bytes, "
                    f"above the {settings.max_document_bytes} byte limit Textract accepts "
                    "for a synchronous read"
                )

    to_read = sum(1 for document in case.documents if document.needs_ocr)
    ctx.state.record(
        "ingest",
        f"accepted {len(case.documents)} document(s), {to_read} to read with OCR",
    )
    return case.documents


def _unread(
    ctx: StepContext[CaseState, DetectorDeps, RawDocument],
    raw: RawDocument,
    reason: str,
) -> RawDocument:
    """Send a document on with no text, and say why."""
    ctx.state.record(
        "ocr", reason, document_id=raw.document_id, document_type=DocumentType.UNKNOWN
    )
    return raw.model_copy(update={"ocr_error": reason, "content": None})


@builder.step
async def ocr(
    ctx: StepContext[CaseState, DetectorDeps, RawDocument],
) -> RawDocument:
    """Read one document with Textract, unless its text was supplied already.

    The step sits inside the fan-out, so it runs once per document at the same time as
    every other document: N `DetectDocumentText` calls in flight rather than N in a row.
    The concurrency belongs to the graph edge; the ceiling on it is the semaphore in
    `TextractOCR`.

    A failure degrades one document, not the case. It travels on with `text` still empty
    and `ocr_error` set, `classify` sends it to `unknown` without spending a model call on
    it, and it reaches the examiner as a finding rather than a 500.
    """
    raw = ctx.inputs

    if not raw.needs_ocr:
        return raw
    if not raw.content:
        return _unread(ctx, raw, "no text supplied, and no bytes to read")
    if ctx.deps.textract is None:
        return _unread(ctx, raw, "no text supplied, and OCR is not configured")

    started = perf_counter()
    try:
        read = await ctx.deps.textract.read(raw.content)
    except Exception as exc:  # noqa: BLE001 - one unreadable document is a finding
        # Deliberately broad. `OcrError` and botocore's own errors are the expected
        # failures, but a real client also raises timeouts, connection resets and parse
        # errors, and every one of them means the same thing here: this document could
        # not be read. Letting any of them escape would fail a whole presentation over
        # one bad scan.
        return _unread(ctx, raw, f"could not be read: {exc}")

    if read.is_empty:
        return _unread(
            ctx,
            raw,
            f"no text found in {read.page_count} page(s); the scan may be blank or illegible",
        )

    ctx.state.record(
        "ocr",
        f"read {read.page_count} page(s), {len(read.text)} characters "
        f"in {perf_counter() - started:.2f}s",
        document_id=raw.document_id,
    )
    return raw.model_copy(
        update={"text": read.text, "page_count": read.page_count, "content": None}
    )


def _unclassified(raw: RawDocument, reason: str) -> RoutedDocument:
    """A document that will skip extraction, and why."""
    return RoutedDocument(
        raw=raw,
        classification=Classification(
            document_type=DocumentType.UNKNOWN, confidence=0.0, reasoning=reason
        ),
    )


@builder.step
async def classify(
    ctx: StepContext[CaseState, DetectorDeps, RawDocument],
) -> RoutedDocument:
    """Decide which document family this text belongs to.

    The verdict alone decides where the document goes next, so anything that should not
    be extracted leaves here as `unknown` rather than as a family the router would then
    have to second-guess.

    A failure degrades one document instead of failing the case: the rest are still
    reconciled, and the gap reaches the examiner as a finding rather than a 500.
    """
    raw = ctx.inputs
    deps = ctx.deps

    if raw.needs_ocr:
        reason = raw.ocr_error or "no text to classify"
        ctx.state.record(
            "classify",
            f"skipped: {reason}",
            document_id=raw.document_id,
            document_type=DocumentType.UNKNOWN,
        )
        return _unclassified(raw, reason)

    try:
        result = await deps.agents.classifier.run(
            _document_prompt(raw), usage_limits=deps.usage_limits
        )
    except (UsageLimitExceeded, RunCancelled):
        # Case-level: the budget is spent, or the whole run is going away.
        raise
    except Exception as exc:  # noqa: BLE001 - one unclassifiable document is a finding
        ctx.state.record(
            "classify",
            f"classification failed: {exc}",
            document_id=raw.document_id,
            document_type=DocumentType.UNKNOWN,
        )
        return _unclassified(raw, f"classification failed: {exc}")

    classification = result.output
    usage = TokenUsage.from_run_usage(result.usage)
    threshold = deps.settings.min_classification_confidence

    if classification.confidence < threshold:
        guess = classification.document_type.value
        message = (
            f"classified as {guess} at {classification.confidence:.2f}, below the "
            f"{threshold:.2f} threshold; treating as unclassified"
        )
        ctx.state.record(
            "classify",
            message,
            document_id=raw.document_id,
            document_type=DocumentType.UNKNOWN,
            usage=result.usage,
        )
        # Forced to unknown rather than merely flagged: reading a document under the
        # wrong schema invents fields, and an invented field becomes a discrepancy.
        return RoutedDocument(
            raw=raw,
            classification=classification.model_copy(
                update={
                    "document_type": DocumentType.UNKNOWN,
                    "reasoning": f"{message}. Original reasoning: {classification.reasoning}",
                }
            ),
            usage=usage,
        )

    ctx.state.record(
        "classify",
        f"classified as {classification.document_type.value} at {classification.confidence:.2f}",
        document_id=raw.document_id,
        document_type=classification.document_type,
        usage=result.usage,
    )
    return RoutedDocument(raw=raw, classification=classification, usage=usage)


def _extraction_step(
    document_type: DocumentType,
) -> Step[CaseState, DetectorDeps, Any, ExtractedDocument]:
    """Build the graph step that extracts one document family.

    The families differ only in their agent and their payload type, so the step bodies
    are generated rather than written out eight times. Each still gets its own node id,
    which is what shows up in the rendered graph and in the instrumentation spans.
    """

    async def extract(
        ctx: StepContext[CaseState, DetectorDeps, RoutedDocument],
    ) -> ExtractedDocument:
        routed = ctx.inputs
        raw = routed.raw
        payload: Any = None
        error: str | None = None
        usage = routed.usage

        try:
            result = await ctx.deps.agents.extractor(document_type).run(
                _document_prompt(raw), usage_limits=ctx.deps.usage_limits
            )
        except (UsageLimitExceeded, RunCancelled):
            # Case-level: the budget is spent, or the whole run is going away.
            raise
        except Exception as exc:  # noqa: BLE001 - one document failing is a finding
            error = f"extraction failed: {exc}"
            ctx.state.record(
                "extract", error, document_id=raw.document_id, document_type=document_type
            )
        else:
            payload = result.output
            usage += TokenUsage.from_run_usage(result.usage)
            ctx.state.record(
                "extract",
                f"extracted {document_type.value}",
                document_id=raw.document_id,
                document_type=document_type,
                usage=result.usage,
            )

        return ExtractedDocument(
            document_id=raw.document_id,
            filename=raw.filename,
            page_count=raw.page_count,
            document_type=document_type,
            confidence=routed.classification.confidence,
            classification_reasoning=routed.classification.reasoning,
            payload=payload,
            error=error,
            usage=usage,
        )

    return builder.step(
        extract,
        node_id=f"extract_{document_type.value}",
        label=document_type.value.replace("_", " "),
    )


EXTRACTION_STEPS: dict[DocumentType, Step[CaseState, DetectorDeps, Any, ExtractedDocument]] = {
    document_type: _extraction_step(document_type)
    for document_type in EXTRACTION_PAYLOAD_TYPES
}
"""One extraction step per document family, keyed by the classifier's verdict.

Generated from the same table that defines the payload types, so a new family needs an
entry there and a prompt, and its step, its route and its span appear on their own.
"""


@builder.step(node_id="skip_unclassified", label="unknown")
async def skip_unclassified(
    ctx: StepContext[CaseState, DetectorDeps, RoutedDocument],
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
        page_count=routed.raw.page_count,
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
        try:
            result = await ctx.deps.agents.reconciler.run(
                _reconciliation_prompt(state, usable),
                usage_limits=ctx.deps.usage_limits,
            )
        except (UsageLimitExceeded, RunCancelled):
            # Case-level: the budget is spent, or the whole run is going away.
            raise
        except Exception as exc:  # noqa: BLE001 - the extractions are still worth returning
            # Every document has already been read, classified and extracted by this
            # point. Letting the failure escape would throw all of that away and hand
            # the caller nothing, so the extracted fields go back with a report saying
            # the cross-checks did not run — which a human examiner can act on.
            report = _unreconciled_report(documents, exc)
            state.record("reconcile", f"reconciliation failed: {exc}")
        else:
            report = _finalise_report(result.output, documents)
            state.record(
                "reconcile",
                f"{report.status.value}: {report.critical_count} critical, "
                f"{report.warning_count} warning",
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


def _unreconciled_report(
    documents: Sequence[ExtractedDocument], error: Exception
) -> ReconciliationReport:
    """The verdict when the documents were read but the cross-checks could not run.

    Not `clean`: nothing was checked, and a presentation nobody compared is exactly the
    one an examiner must look at by hand.
    """
    usable = sum(1 for document in documents if document.is_usable)
    return ReconciliationReport(
        status=CaseStatus.NEEDS_REVIEW,
        summary=(
            f"{usable} document(s) were read successfully, but the compliance check could "
            "not be completed. The extracted fields are included below; no cross-document "
            "comparison was performed."
        ),
        mismatches=[
            Mismatch(
                code="reconciliation_failed",
                severity=Severity.WARNING,
                field="presentation",
                explanation=f"The reconciliation step failed: {error}",
                documents_involved=[document.document_type for document in documents],
                suggested_action="Retry the case, or examine the extracted fields manually.",
            )
        ],
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


def _routing_decision() -> Decision[CaseState, DetectorDeps, RoutedDocument]:
    """The branch table from a classified document to its extractor.

    Built by walking `EXTRACTION_STEPS`, so the routes cannot fall out of step with the
    steps they point at: there is no list of branches to forget to update. Anything the
    classifier could not place — including a guess below the confidence threshold —
    arrives as `unknown` and takes the last branch.
    """
    decision = builder.decision(node_id="route_by_document_type", note="UCP 600 document families")
    for document_type, step in EXTRACTION_STEPS.items():
        decision = decision.branch(
            builder.match(RoutedDocument, matches=_is_type(document_type)).to(step)
        )
    return decision.branch(
        builder.match(RoutedDocument, matches=_is_type(DocumentType.UNKNOWN)).to(skip_unclassified)
    )


def _is_type(document_type: DocumentType) -> Callable[[RoutedDocument], bool]:
    """Match one document family. A closure, so each branch captures its own type."""
    return lambda routed: routed.document_type is document_type


builder.add(
    builder.edge_from(builder.start_node).to(ingest),
    builder.edge_from(ingest)
    .label("per document")
    .map(fork_id=FAN_OUT_ID, downstream_join_id=COLLECT_ID)
    .to(ocr),
    builder.edge_from(ocr).to(classify),
    builder.edge_from(classify).to(_routing_decision()),
    builder.edge_from(*EXTRACTION_STEPS.values(), skip_unclassified).to(collect),
    builder.edge_from(collect).label("all documents").to(reconcile),
    builder.edge_from(reconcile).to(builder.end_node),
)

case_graph: Graph[CaseState, DetectorDeps, CaseInput, CaseResult] = builder.build()
"""The built graph. Stateless and safe to share across concurrent runs."""


def render_mermaid(title: str | None = None) -> str:
    """The pipeline as a Mermaid diagram, for docs and for debugging the wiring."""
    return case_graph.render(title=title)
