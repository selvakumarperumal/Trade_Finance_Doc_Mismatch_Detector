"""Reading documents: page splitting, format sniffing, and how a bad file is refused."""

from __future__ import annotations

import asyncio

import pytest

from Detector.services.ocr import (
    PDF_MEDIA_TYPE,
    OcrError,
    TextractOCR,
    sniff_media_type,
    split_pdf_pages,
)
from tests.conftest import PNG, StubTextract, make_pdf


def test_splits_a_pdf_into_one_file_per_page() -> None:
    pages = split_pdf_pages(make_pdf(4), max_pages=30)
    assert len(pages) == 4
    assert all(sniff_media_type(p) == PDF_MEDIA_TYPE for p in pages), 'each piece is a PDF'


@pytest.mark.parametrize(
    ('content', 'max_pages', 'reason'),
    [
        (make_pdf(4), 2, 'above the limit'),
        (b'not a pdf at all', 30, 'not a readable PDF'),
        (b'', 30, 'not a readable PDF'),
    ],
)
def test_refuses_pdfs_it_cannot_use(content: bytes, max_pages: int, reason: str) -> None:
    with pytest.raises(OcrError, match=reason):
        split_pdf_pages(content, max_pages=max_pages)


@pytest.mark.parametrize(
    ('content', 'expected'),
    [
        (b'%PDF-1.7 ...', 'application/pdf'),
        (PNG, 'image/png'),
        (b'\xff\xd8\xff\xe0', 'image/jpeg'),
        (b'II*\x00', 'image/tiff'),
        (b'MM\x00*', 'image/tiff'),
        (b'MZ\x90\x00', None),
        (b'', None),
    ],
)
def test_identifies_a_document_from_its_bytes(content: bytes, expected: str | None) -> None:
    assert sniff_media_type(content) == expected


@pytest.mark.asyncio
async def test_reads_every_page_and_keeps_them_in_order() -> None:
    class Counting(StubTextract):
        async def detect_document_text(self, Document: dict) -> dict:
            self.calls += 1
            return {'Blocks': [{'BlockType': 'LINE', 'Text': f'page {self.calls}'}]}

    stub = Counting()
    reader = TextractOCR(client=stub, limit=asyncio.Semaphore(4), page_separator='|')
    result = await reader.read(make_pdf(3))

    assert stub.calls == 3, 'one Textract call per page'
    assert result.page_count == 3
    assert result.text.split('|') == ['page 1', 'page 2', 'page 3']


@pytest.mark.asyncio
async def test_reads_a_single_page_image_in_one_call(stub_textract: StubTextract) -> None:
    reader = TextractOCR(client=stub_textract, limit=asyncio.Semaphore(4))
    result = await reader.read(PNG)

    assert stub_textract.calls == 1
    assert result.page_count == 1
    assert not result.is_empty


@pytest.mark.asyncio
async def test_the_bytes_decide_what_a_document_is(stub_textract: StubTextract) -> None:
    """Nothing tells the reader what a file is; it looks."""
    reader = TextractOCR(client=stub_textract, limit=asyncio.Semaphore(4))

    assert (await reader.read(make_pdf(2))).page_count == 2

    with pytest.raises(OcrError, match='not a document Textract can read'):
        await reader.read(b'MZ\x90\x00' + b'x' * 100)


@pytest.mark.asyncio
async def test_empty_scan_is_reported_rather_than_passed_on() -> None:
    reader = TextractOCR(client=StubTextract(text='   '), limit=asyncio.Semaphore(4))
    assert (await reader.read(PNG)).is_empty
