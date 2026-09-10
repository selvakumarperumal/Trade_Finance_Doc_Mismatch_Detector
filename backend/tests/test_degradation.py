"""One thing failing must not lose the rest.

Every stage of the pipeline is meant to degrade rather than fail the case: a scan that
cannot be read, a document that cannot be classified, an extraction that errors, and a
reconciliation step that dies after everything else succeeded. These are the tests that
say so, because each one was a bug.

Case-level failures — the token budget, cancellation — must still stop the run, and
that is tested here too, since a handler broad enough to catch everything else could
easily swallow those by mistake.
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack

import pytest
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from Detector.core.config import Settings
from Detector.models.documents import CaseInput, RawDocument
from Detector.models.enums import CaseStatus, DocumentType
from Detector.services.deps import DetectorDeps
from Detector.services.ocr import TextractOCR
from Detector.services.pipeline import DetectorPipeline
from Detector.services.runner import CaseRunner
from Detector.services.store import CaseStore

pytestmark = pytest.mark.asyncio

INVOICE = {'document_type': 'commercial_invoice', 'confidence': 0.95, 'reasoning': 'invoice markers'}
CLEAN_REPORT = {
    'status': 'clean',
    'summary': 'ok',
    'mismatches': [],
    'matched_fields': [],
    'missing_documents': [],
}


def text_case(count: int = 3, case_id: str = 'c1') -> CaseInput:
    return CaseInput(
        case_id=case_id,
        documents=[
            RawDocument(document_id=f'd{i}', text=f'COMMERCIAL INVOICE {i}') for i in range(count)
        ],
    )


def deps_for(settings: Settings) -> DetectorDeps:
    return DetectorDeps.default(settings=settings)


def raising(exc: Exception):
    """A model that fails the way a real provider client can, outside `AgentRunError`."""

    def call(messages, info):
        raise exc

    return FunctionModel(call)


async def test_a_reader_blowing_up_degrades_one_document(settings: Settings) -> None:
    """A `RuntimeError` from the OCR client is not a botocore error, and used to escape."""

    class Flaky:
        def __init__(self) -> None:
            self.calls = 0

        async def detect_document_text(self, Document: dict) -> dict:
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError('textract exploded')
            return {'Blocks': [{'BlockType': 'LINE', 'Text': 'COMMERCIAL INVOICE 1'}]}

    from dataclasses import replace

    deps = replace(
        deps_for(settings), textract=TextractOCR(client=Flaky(), limit=asyncio.Semaphore(1))
    )
    case = CaseInput(
        case_id='ocr',
        documents=[RawDocument(document_id=f'd{i}', content=b'%PDF-fake') for i in range(3)],
    )
    with (
        deps.agents.classifier.override(model=TestModel(custom_output_args=INVOICE)),
        deps.agents.reconciler.override(model=TestModel(call_tools=[], custom_output_args=CLEAN_REPORT)),
    ):
        result = await DetectorPipeline(deps).run(case)

    assert result.status is not None, 'the case must finish rather than raise'
    assert len(result.documents) == 3, 'every document is accounted for'


async def test_a_classifier_failure_degrades_one_document(settings: Settings) -> None:
    deps = deps_for(settings)
    with (
        deps.agents.classifier.override(model=raising(RuntimeError('provider reset'))),
        deps.agents.reconciler.override(model=TestModel(call_tools=[], custom_output_args=CLEAN_REPORT)),
    ):
        result = await DetectorPipeline(deps).run(text_case(2))

    assert all(d.document_type is DocumentType.UNKNOWN for d in result.documents)
    assert all('classification failed' in (d.classification_reasoning or '') for d in result.documents)


async def test_an_extraction_failure_degrades_one_document(settings: Settings) -> None:
    deps = deps_for(settings)
    with ExitStack() as stack:
        stack.enter_context(deps.agents.classifier.override(model=TestModel(custom_output_args=INVOICE)))
        stack.enter_context(
            deps.agents.extractors[DocumentType.COMMERCIAL_INVOICE].override(
                model=raising(RuntimeError('extractor died'))
            )
        )
        stack.enter_context(
            deps.agents.reconciler.override(
                model=TestModel(call_tools=[], custom_output_args=CLEAN_REPORT)
            )
        )
        result = await DetectorPipeline(deps).run(text_case(2))

    assert not any(d.is_usable for d in result.documents)
    assert all('extraction failed' in (d.error or '') for d in result.documents)
    assert result.status is CaseStatus.NEEDS_REVIEW, 'nothing was checked, so a human must look'


async def test_a_reconciler_failure_keeps_the_extractions(settings: Settings) -> None:
    """Every document was already read and extracted; that work must not be thrown away."""
    deps = deps_for(settings)
    with ExitStack() as stack:
        stack.enter_context(deps.agents.classifier.override(model=TestModel(custom_output_args=INVOICE)))
        stack.enter_context(deps.agents.reconciler.override(model=raising(RuntimeError('melted'))))
        result = await DetectorPipeline(deps).run(text_case(3))

    assert len(result.documents) == 3
    assert all(d.is_usable for d in result.documents), 'the extracted payloads survive'
    assert result.status is CaseStatus.NEEDS_REVIEW
    assert [m.code for m in result.report.mismatches] == ['reconciliation_failed']


async def test_a_spent_budget_still_stops_the_case(settings: Settings) -> None:
    """The broad handlers above must not swallow a genuinely case-level failure."""
    deps = deps_for(settings)
    with ExitStack() as stack:
        stack.enter_context(deps.agents.classifier.override(model=TestModel(custom_output_args=INVOICE)))
        stack.enter_context(
            deps.agents.reconciler.override(model=raising(UsageLimitExceeded('budget spent')))
        )
        with pytest.raises(UsageLimitExceeded):
            await DetectorPipeline(deps).run(text_case(2))


async def test_an_unreadable_document_overrides_a_clean_verdict(settings: Settings) -> None:
    deps = deps_for(settings)
    case = CaseInput(
        case_id='mixed',
        documents=[
            RawDocument(document_id='good', text='COMMERCIAL INVOICE 1'),
            RawDocument(document_id='unreadable', text='   ', content=b'not a document'),
        ],
    )
    with (
        deps.agents.classifier.override(model=TestModel(custom_output_args=INVOICE)),
        deps.agents.reconciler.override(model=TestModel(call_tools=[], custom_output_args=CLEAN_REPORT)),
    ):
        result = await DetectorPipeline(deps).run(case)

    assert result.status is CaseStatus.NEEDS_REVIEW, 'the model said clean; the rule overrides it'
    assert 'document_not_extracted' in [m.code for m in result.report.mismatches]


async def test_deleting_a_running_case_leaves_no_stray_task_error(settings: Settings) -> None:
    """The record vanishing mid-run is a normal end, not an unretrieved task exception."""
    failures: list[str] = []
    asyncio.get_running_loop().set_exception_handler(
        lambda loop, context: failures.append(repr(context.get('exception')))
    )
    runner = CaseRunner.build(DetectorPipeline.default(settings), CaseStore(settings), settings)
    try:
        await runner.submit(text_case(3, case_id='vanish'))
        await asyncio.sleep(0)
        await runner.store.forget('vanish')
        await asyncio.sleep(0.3)
    finally:
        await runner.aclose(grace=2)
        asyncio.get_running_loop().set_exception_handler(None)

    assert failures == [], f'the run should end quietly, got {failures}'
