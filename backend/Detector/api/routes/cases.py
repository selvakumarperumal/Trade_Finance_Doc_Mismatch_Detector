"""Submitting a presentation, and collecting the answer.

The shape follows from one fact: analysing a presentation takes minutes. That is longer
than a browser, a load balancer or a person will hold a connection open, so submitting a
case and collecting its result are two separate acts:

    POST   /v1/cases/uploads      the files              -> 202 CaseRecord
    POST   /v1/cases              already-extracted text -> 202 CaseRecord
    GET    /v1/cases/{id}         poll it
    DELETE /v1/cases/{id}         give up on it, and forget what it held

Every one of them answers with the same `CaseRecord`, which carries the status, the audit
trail and — once it is done — the report.

Watching a case as it runs is the other half, and it lives in `api/events.py`: a
Socket.IO client subscribes and gets that same `CaseRecord` pushed on every change.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, File, Form, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field

from Detector.api.dependencies import RunnerDep, SettingsDep
from Detector.api.errors import ErrorResponse, UploadRejected
from Detector.core.config import Settings
from Detector.models.documents import CaseInput, RawDocument
from Detector.models.jobs import CaseRecord
from Detector.services.ocr import SUPPORTED_MEDIA_TYPES, sniff_media_type

router = APIRouter(prefix='/v1/cases', tags=['cases'])

UPLOAD_CHUNK_BYTES = 1 << 20
"""Read uploads a megabyte at a time, so the size limits can be enforced *while* the
bytes arrive rather than after all of them are already in memory."""

ERRORS: dict[int | str, dict[str, type[ErrorResponse]]] = {
    code: {'model': ErrorResponse} for code in (404, 409, 413, 415, 422, 503)
}
"""The ways these routes refuse, for the OpenAPI schema."""


class DocumentInput(BaseModel):
    """One presented document, as text the caller has already extracted."""

    model_config = ConfigDict(extra='forbid')

    text: str = Field(min_length=1)
    document_id: str | None = Field(default=None, description='Generated from position if omitted.')
    filename: str | None = None


class AnalyseRequest(BaseModel):
    """A presentation whose documents arrive as text rather than as files."""

    model_config = ConfigDict(extra='forbid')

    documents: list[DocumentInput] = Field(min_length=1)
    case_id: str | None = Field(default=None, description='Generated if omitted.')
    presented_on: date | None = Field(
        default=None,
        description=(
            'The date the documents reached the bank. Without it the UCP 600 Art 14(c) '
            'presentation-period check is skipped rather than guessed.'
        ),
    )

    def to_case_input(self) -> CaseInput:
        """The domain object the pipeline runs on."""
        return CaseInput(
            case_id=self.case_id or new_case_id(),
            presented_on=self.presented_on,
            documents=[
                RawDocument(
                    document_id=document.document_id or f'doc-{index}',
                    text=document.text,
                    filename=document.filename,
                )
                for index, document in enumerate(self.documents, start=1)
            ],
        )


def new_case_id() -> str:
    """A case id for a caller that did not bring one."""
    return f'case-{uuid4().hex[:12]}'


# --- submitting --------------------------------------------------------------


@router.post(
    '/uploads',
    status_code=status.HTTP_202_ACCEPTED,
    summary='Submit a presentation as uploaded files',
    responses=ERRORS,
)
async def submit_uploads(
    runner: RunnerDep,
    settings: SettingsDep,
    files: Annotated[
        list[UploadFile], File(description='One file per document: PDF, PNG, JPEG or TIFF.')
    ],
    case_id: Annotated[str | None, Form()] = None,
    presented_on: Annotated[date | None, Form()] = None,
) -> CaseRecord:
    """Take a pile of scanned documents and start analysing them.

    This is the submit button: send the credit, the invoice, the bill of lading and
    whatever else was presented in one request, and get back a case id. Each file is then
    read with Textract on its own branch of the graph, classified, extracted under its
    family's schema, and finally reconciled against the others.

    Nothing is written to disk or to a bucket — the bytes live in memory until the
    document has been read, then are dropped.
    """
    documents = await _read_uploads(files, settings)
    return await runner.submit(
        CaseInput(
            case_id=case_id or new_case_id(),
            presented_on=presented_on,
            documents=documents,
        )
    )


@router.post(
    '',
    status_code=status.HTTP_202_ACCEPTED,
    summary='Submit a presentation as already-extracted text',
    responses=ERRORS,
)
async def submit_case(request: AnalyseRequest, runner: RunnerDep) -> CaseRecord:
    """Start analysing a presentation whose text was extracted upstream.

    The same pipeline as `/uploads` minus the reading step. Scans belong on `/uploads`.
    """
    return await runner.submit(request.to_case_input())


# --- collecting --------------------------------------------------------------


@router.get('/{case_id}', summary='Where a case has got to', responses=ERRORS)
async def get_case(case_id: str, runner: RunnerDep) -> CaseRecord:
    """Poll a submitted case.

    `status` moves `queued` -> `running` -> `succeeded` or `failed`. On `succeeded`,
    `result` holds the report; on `failed`, `error_code` and `error` say why.
    """
    return await runner.get(case_id)


@router.delete(
    '/{case_id}',
    status_code=status.HTTP_204_NO_CONTENT,
    summary='Abandon a case and forget what it held',
    responses=ERRORS,
)
async def delete_case(case_id: str, runner: RunnerDep) -> Response:
    """Cancel the analysis if it is still running, then drop the record.

    A record holds the extracted contents of a presentation — counterparty names, bank
    details, amounts — so a caller that has collected its result can have it dropped now
    rather than waiting for the retention window.
    """
    await runner.discard(case_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- reading the uploads -----------------------------------------------------


async def _read_uploads(files: list[UploadFile], settings: Settings) -> list[RawDocument]:
    """Read every uploaded file into memory, refusing what cannot be used.

    Each check runs at the first moment it becomes knowable: the file count before a byte
    is read, the size while the bytes arrive, and the format from the bytes themselves.
    `remaining` is what is left of the case-wide budget, so each file is measured against
    what is actually available rather than against the per-file limit alone.
    """
    _check_count(files, settings)

    documents: list[RawDocument] = []
    remaining = settings.max_case_bytes
    for index, upload in enumerate(files, start=1):
        limit = min(settings.max_document_bytes, remaining)
        content = await _read_upload(upload, settings, limit=limit)
        remaining -= len(content)
        documents.append(
            RawDocument(
                document_id=f'doc-{index}',
                filename=upload.filename,
                content=content,
            )
        )
    return documents


def _check_count(files: list[UploadFile], settings: Settings) -> None:
    """Refuse an unusable batch before reading a single byte.

    The document limit is enforced in the graph too, but reaching it there means every
    file has already been read into memory first.
    """
    if not files:
        raise UploadRejected('no files were uploaded')
    if len(files) > settings.max_documents_per_case:
        raise UploadRejected(
            f'{len(files)} files were uploaded, above the limit of '
            f'{settings.max_documents_per_case} documents per case'
        )


async def _read_upload(upload: UploadFile, settings: Settings, *, limit: int) -> bytes:
    """Read one upload into memory, stopping as soon as it is clear it cannot be used.

    Three ways to be refused, in the order they become knowable: past `limit` bytes,
    empty, or not a format Textract reads. The format check comes last because it needs
    the leading bytes — but it still happens before anything is sent to AWS, and it reads
    those bytes rather than trusting the content type the browser claimed.
    """
    name = upload.filename or 'an unnamed file'

    chunks: list[bytes] = []
    size = 0
    while chunk := await upload.read(UPLOAD_CHUNK_BYTES):
        size += len(chunk)
        if size > limit:
            raise UploadRejected(
                f'{name} is larger than the {limit} bytes still available for this case '
                f'(per-file limit {settings.max_document_bytes}, '
                f'per-case limit {settings.max_case_bytes})',
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                code='upload_too_large',
            )
        chunks.append(chunk)

    content = b''.join(chunks)
    if not content:
        raise UploadRejected(f'{name} is empty')
    if sniff_media_type(content) is None:
        raise UploadRejected(
            f'{name} is not a document this service can read; accepted types are '
            f'{", ".join(sorted(SUPPORTED_MEDIA_TYPES))}, and the bytes match none of them '
            f'(the browser called it {upload.content_type or "nothing in particular"})',
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            code='unsupported_media_type',
        )
    return content
