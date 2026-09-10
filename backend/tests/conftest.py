"""Shared fixtures.

Everything here runs without an API key and without AWS. The agents are built on
pydantic-ai's `test` model, and Textract is a stub, so the suite exercises the wiring —
routing, fan-out, limits, the job lifecycle — rather than the models' judgement.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import replace
from io import BytesIO
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from Detector.api.app import create_app
from Detector.core.config import Settings
from Detector.services.ocr import TextractOCR
from Detector.services.pipeline import DetectorPipeline


def make_pdf(pages: int = 1, pad: int = 0) -> bytes:
    """A syntactically valid PDF with `pages` blank pages, optionally padded out."""
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue() + (b'%' + b'x' * pad if pad else b'')


PNG = b'\x89PNG\r\n\x1a\n' + b'x' * 64
"""Enough of a PNG header to be recognised; the stub reader never looks further."""


class StubTextract:
    """Stands in for the Textract client, one line of text per call."""

    def __init__(self, text: str = 'COMMERCIAL INVOICE 12345 TOTAL 100.00') -> None:
        self.text = text
        self.calls = 0

    async def detect_document_text(self, Document: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        return {
            'Blocks': [
                {'BlockType': 'LINE', 'Text': self.text},
                {'BlockType': 'WORD', 'Text': 'ignored'},
            ]
        }


@pytest.fixture
def settings() -> Settings:
    """Test settings: no telemetry, no real provider, small limits so they are reachable."""
    return Settings(
        logfire_enabled=False,
        ocr_enabled=False,
        classifier_model='test',
        extraction_model='test',
        reconciliation_model='test',
        max_documents_per_case=3,
        max_document_bytes=4_000,
        max_case_bytes=9_000,
    )


@pytest.fixture
def stub_textract() -> StubTextract:
    return StubTextract()


@pytest.fixture
def client(settings: Settings, stub_textract: StubTextract) -> Iterator[TestClient]:
    """An app with a stubbed reader, started through its real lifespan."""
    app = create_app(settings)
    with TestClient(app) as test_client:
        app.state.runner.pipeline = DetectorPipeline(
            deps=replace(
                app.state.runner.pipeline.deps,
                textract=TextractOCR(client=stub_textract, limit=asyncio.Semaphore(4)),
            )
        )
        yield test_client
