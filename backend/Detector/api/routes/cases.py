"""Submitting a presentation, and collecting the answer.

The shape follows from one fact: analysing a presentation takes minutes. That is longer
than a browser, a load balancer or a person will hold a connection open, so submitting a
case and collecting its result are two separate acts:

    POST   /v1/cases/uploads      the files              -> 202 CaseRecord
    POST   /v1/cases              already-extracted text -> 202 CaseRecord
    WS     /v1/cases/{id}/stream  watch it happen
    GET    /v1/cases/{id}         poll it instead
    DELETE /v1/cases/{id}         give up on it, and forget what it held

Every one of them answers with the same `CaseRecord`, which carries the status, the
audit trail and — once it is done — the report. The websocket sends the whole record on
each change rather than a delta, so a client renders the latest one and is correct
whether it connected before the first document was read or after the last.

The upload route refuses what it cannot use *before* spending anything on it: the file
count before reading a byte, the running total as it reads, and the format from the
bytes rather than from what the browser claimed.
"""

from __future__ import annotations

from contextlib import suppress
from datetime import date
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, File, Form, Response, UploadFile, WebSocket, status
from pydantic import BaseModel, ConfigDict, Field
from starlette.websockets import WebSocketDisconnect

from Detector.api.dependencies import RunnerDep, SettingsDep
from Detector.api.errors import ErrorResponse, UploadRejected
from Detector.core.config import Settings
from Detector.models.documents import CaseInput, RawDocument
from Detector.models.jobs import CaseRecord
from Detector.services.ocr import SUPPORTED_MEDIA_TYPES, sniff_media_type
from Detector.services.store import CaseNotFound

router = APIRouter(prefix='/v1/cases', tags=['cases'])

UPLOAD_CHUNK_BYTES = 1 << 20
"""Read uploads a megabyte at a time, so the case-wide size limit can be enforced
*while* the bytes arrive rather than after all of them are already in memory."""

ERRORS: dict[int | str, dict[str, type[ErrorResponse]]] = {
    code: {'model': ErrorResponse} for code in (404, 409, 413, 415, 422, 503)
}


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
    _check_count(files, settings)

    documents: list[RawDocument] = []
    remaining = settings.max_case_bytes
    for index, upload in enumerate(files, start=1):
        content = await _read_upload(upload, index, settings, budget=remaining)
        remaining -= len(content)
        documents.append(
            RawDocument(
                document_id=f'doc-{index}',
                filename=upload.filename,
                content=content,
            )
        )

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
    return await runner.store.get(case_id)


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
    await runner.store.get(case_id)  # 404 rather than a silent success on an unknown id.
    await runner.cancel(case_id)
    await runner.store.forget(case_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.websocket('/{case_id}/stream')
async def stream_case(case_id: str, websocket: WebSocket, runner: RunnerDep) -> None:
    """Watch a submitted case run.

    Connect with the id the submission returned. Each message is a complete `CaseRecord`
    — the same shape `GET /v1/cases/{case_id}` returns — sent on every change, so there
    is no race between the POST returning and the socket opening, and a reconnect needs
    no cursor. The stream ends when `status` is terminal.

    Events arrive in completion order rather than document order, because the documents
    are read, classified and extracted in parallel. Key your UI on `document_id`.
    """
    await websocket.accept()
    try:
        async for record in runner.store.watch(case_id):
            await websocket.send_text(record.model_dump_json())
    except CaseNotFound as exc:
        await websocket.send_json({'code': 'case_not_found', 'detail': str(exc)})
    except WebSocketDisconnect:
        return  # The client went away; nothing to report to.
    finally:
        # The client may already have gone, which closing again would complain about.
        with suppress(RuntimeError):
            await websocket.close()


# --- upload validation -------------------------------------------------------


def _check_count(files: list[UploadFile], settings: Settings) -> None:
    """Refuse an unusable batch before reading a single byte.

    The document limit is enforced in the graph too, but reaching it there means every
    file has already been read into memory first.
    """
    if not files:
        raise UploadRejected(
            'no files were uploaded',
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code='invalid_case',
        )
    if len(files) > settings.max_documents_per_case:
        raise UploadRejected(
            f'{len(files)} files were uploaded, above the limit of '
            f'{settings.max_documents_per_case} documents per case',
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code='invalid_case',
        )


async def _read_upload(
    upload: UploadFile, index: int, settings: Settings, *, budget: int
) -> bytes:
    """Read one upload into memory, stopping as soon as it is clear it cannot be used.

    Three ways to be refused, in the order they become knowable: past the per-file limit,
    past what is left of the case's budget, or not a format Textract reads. The format
    check comes last because it needs the first bytes, but it still happens before
    anything is sent to AWS.
    """
    label = upload.filename or f'file {index}'
    ceiling = min(settings.max_document_bytes, max(budget, 0))

    chunks: list[bytes] = []
    size = 0
    while chunk := await upload.read(UPLOAD_CHUNK_BYTES):
        size += len(chunk)
        if size > ceiling:
            raise UploadRejected(
                f'{label} is larger than the {ceiling} bytes still available for this case '
                f'(per-file limit {settings.max_document_bytes}, '
                f'per-case limit {settings.max_case_bytes})',
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                code='upload_too_large',
            )
        chunks.append(chunk)

    content = b''.join(chunks)
    if not content:
        raise UploadRejected(
            f'{label} is empty',
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code='invalid_case',
        )
    if sniff_media_type(content) is None:
        raise UploadRejected(
            f'{label} is not a document this service can read; accepted types are '
            f'{", ".join(sorted(SUPPORTED_MEDIA_TYPES))}, and the bytes match none of them '
            f'(the browser called it {upload.content_type or "nothing in particular"})',
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            code='unsupported_media_type',
        )
    return content
