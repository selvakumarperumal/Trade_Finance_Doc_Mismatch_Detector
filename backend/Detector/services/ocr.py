"""Reading a document's bytes with Amazon Textract.

`DetectDocumentText` is Textract's synchronous read: hand it the bytes of a page, get
back blocks of lines and words, with no job to submit and poll. The bytes travel inline
in the request and are dropped as soon as the text comes back, so the pipeline stores
nothing.

The price of that is Textract's synchronous limits: **one page**, 10 MB per call. A
presentation is not single pages — a letter of credit runs to three or four, a bill of
lading to two — so a multi-page PDF is split here, one call per page, and the text is
rejoined in page order. The alternative, `StartDocumentTextDetection`, reads a whole PDF
but only from an S3 bucket, which means a bucket, a lifecycle policy and a place where
the documents come to rest. Splitting keeps the bytes in memory and the deployment to
one service.

Two objects, because they have two lifetimes. `textract_client()` opens the aioboto3
client, which owns a connection pool and wants to live as long as the process.
`TextractOCR` pairs that client with the semaphore that decides how many pages may be
read at once, which is a per-deployment policy.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any, Final

import aioboto3
from botocore.config import Config as BotoConfig
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PyPdfError

from Detector.core.config import Settings, get_settings

PDF_MEDIA_TYPE: Final = 'application/pdf'

_MAGIC: Final[tuple[tuple[bytes, str], ...]] = (
    (b'%PDF-', PDF_MEDIA_TYPE),
    (b'\x89PNG\r\n\x1a\n', 'image/png'),
    (b'\xff\xd8\xff', 'image/jpeg'),
    (b'II*\x00', 'image/tiff'),
    (b'MM\x00*', 'image/tiff'),
)
"""File signatures for the formats Textract reads, checked in order."""

SUPPORTED_MEDIA_TYPES: Final[frozenset[str]] = frozenset(media for _, media in _MAGIC)
"""Everything the OCR step can read. The upload route refuses the rest at the door."""


class OcrError(Exception):
    """A document could not be read.

    Raised for input the reader can see is unusable — an unsupported media type, a
    corrupt PDF, a page count above the configured ceiling — as opposed to a Textract
    failure, which surfaces as `ClientError`/`BotoCoreError`. Both degrade a single
    document rather than the case; see the `ocr` step in `Detector.services.graph`.
    """


@dataclass(frozen=True, slots=True)
class ReadResult:
    """The text of one document, and how many pages it took to get it."""

    text: str
    page_count: int

    @property
    def is_empty(self) -> bool:
        """Whether Textract found nothing worth passing to a model."""
        return not self.text.strip()


@asynccontextmanager
async def textract_client(settings: Settings | None = None) -> AsyncIterator[Any]:
    """Open one Textract client, and close it again when the block exits.

    Open it once, where the process starts up — a client per page would pay TLS setup on
    every read. In FastAPI that means the lifespan handler; see `Detector.api.app`.
    """
    settings = settings or get_settings()
    session = aioboto3.Session()
    async with session.client(
        'textract',
        region_name=settings.textract_region,
        config=BotoConfig(
            retries={'max_attempts': settings.textract_max_attempts, 'mode': 'adaptive'},
            max_pool_connections=max(settings.max_parallel_ocr_calls, 10),
        ),
    ) as client:
        yield client


def sniff_media_type(content: bytes) -> str | None:
    """Identify a document from its leading bytes, or `None` if it is not one we read.

    The browser's `Content-Type` on a multipart part is whatever the operating system
    guessed from the file extension, and it is routinely `application/octet-stream`. The
    bytes are the only thing that actually says what a file is, so they decide — which
    both accepts correctly-formed uploads that were mislabelled and refuses a renamed
    `.exe` before it costs a round trip to AWS.
    """
    return next((media_type for magic, media_type in _MAGIC if content.startswith(magic)), None)


def split_pdf_pages(content: bytes, *, max_pages: int) -> list[bytes]:
    """Split a PDF into one single-page PDF per page.

    Each page keeps its own resources, so the pieces are valid PDFs that Textract's
    synchronous API will accept. Synchronous and CPU-bound; `TextractOCR.read` calls it
    on a worker thread so it does not stall the event loop while other documents are in
    flight.

    Raises:
        OcrError: the PDF is unreadable, has no pages, or has more than `max_pages`.
    """
    try:
        reader = PdfReader(BytesIO(content))
        page_count = len(reader.pages)
    except (PyPdfError, ValueError, OSError) as exc:
        raise OcrError(f'not a readable PDF: {exc}') from exc

    if reader.is_encrypted:
        # An empty user password is the common case for "protected" bank documents and
        # decrypts silently; a real password is a document we genuinely cannot read.
        try:
            decrypted = reader.decrypt('')
        except (PyPdfError, NotImplementedError) as exc:
            raise OcrError(f'PDF is encrypted and could not be opened: {exc}') from exc
        if not decrypted:
            raise OcrError('PDF is password protected; supply an unprotected copy')

    if page_count == 0:
        raise OcrError('PDF has no pages')
    if page_count > max_pages:
        raise OcrError(
            f'PDF has {page_count} pages, above the limit of {max_pages}; '
            'split it before submitting'
        )

    pages: list[bytes] = []
    for index in range(page_count):
        writer = PdfWriter()
        try:
            writer.add_page(reader.pages[index])
        except (PyPdfError, ValueError) as exc:
            raise OcrError(f'page {index + 1} could not be read: {exc}') from exc
        buffer = BytesIO()
        writer.write(buffer)
        pages.append(buffer.getvalue())
    return pages


@dataclass(frozen=True, slots=True)
class TextractOCR:
    """A Textract client, plus a cap on how many pages it reads at once."""

    client: Any
    """Anything exposing `detect_document_text`; tests pass a stub."""

    limit: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(4))
    """Textract is rate limited per account and region, so neither the fan-out over
    documents nor the fan-out over one document's pages may call it without a ceiling.
    Both draw on this one semaphore, because both spend the same quota."""

    max_pages: int = 30
    """Pages the reader will accept in one document."""

    page_separator: str = '\n\n'
    """Joins consecutive pages into one continuous document.

    Blank rather than marked up: a page banner in the middle of a letter of credit is
    text the model did not read off the paper."""

    @classmethod
    def from_settings(cls, client: Any, settings: Settings | None = None) -> TextractOCR:
        """Pair a client with the configured concurrency ceiling and page limits."""
        settings = settings or get_settings()
        return cls(
            client=client,
            limit=asyncio.Semaphore(settings.max_parallel_ocr_calls),
            max_pages=settings.max_document_pages,
        )

    async def read(self, content: bytes) -> ReadResult:
        """The text of one document, in reading order, however many pages it has.

        The format is taken from the bytes, not from anything the caller was told: a
        browser's `Content-Type` is a guess from the file extension, and a document that
        disagrees with its label is exactly the one worth getting right. A PDF is split
        and its pages read concurrently under the shared semaphore, so a four-page credit
        costs four calls but roughly one call's latency.

        Raises:
            OcrError: not a format Textract can read.
            ClientError | BotoCoreError: Textract itself refused or failed.
        """
        media_type = sniff_media_type(content)
        if media_type is None:
            raise OcrError(
                'not a document Textract can read; supported types are '
                f'{", ".join(sorted(SUPPORTED_MEDIA_TYPES))}'
            )
        if media_type == PDF_MEDIA_TYPE:
            pages = await asyncio.to_thread(split_pdf_pages, content, max_pages=self.max_pages)
            return await self._read_pages(pages)
        return ReadResult(text=await self._read_one(content), page_count=1)

    async def _read_pages(self, pages: Sequence[bytes]) -> ReadResult:
        """Read every page of a split document, keeping them in page order.

        `asyncio.gather` returns results positionally, so the pages rejoin in the order
        they appear in the file rather than the order Textract happened to finish them.
        One page failing fails the document: half a letter of credit read as if it were
        the whole thing is worse than no letter of credit at all, because the missing
        half becomes a confident finding about a field that was simply not read.
        """
        texts = await asyncio.gather(*(self._read_one(page) for page in pages))
        joined = self.page_separator.join(text for text in texts if text.strip())
        return ReadResult(text=joined, page_count=len(pages))

    async def _read_one(self, content: bytes) -> str:
        """One `DetectDocumentText` call, as newline-separated lines in reading order.

        Textract returns a block per page, per line and per word; the LINE blocks already
        arrive in reading order, so this keeps the service's ordering rather than
        re-sorting by geometry.
        """
        async with self.limit:
            response = await self.client.detect_document_text(Document={'Bytes': content})
        return '\n'.join(
            block['Text'] for block in response['Blocks'] if block['BlockType'] == 'LINE'
        )


__all__ = [
    'PDF_MEDIA_TYPE',
    'SUPPORTED_MEDIA_TYPES',
    'OcrError',
    'ReadResult',
    'TextractOCR',
    'sniff_media_type',
    'split_pdf_pages',
    'textract_client',
]
