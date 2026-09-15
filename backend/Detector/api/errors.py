"""Turning refusals into HTTP answers.

Every error leaves as `{"code": ..., "detail": ...}`, so a client can branch on the code
rather than parse prose.

There is not much here, and that is the point. A case is analysed on a background task
long after its submission returned, so a model failure or a spent budget has no request
left to answer: the runner records those codes on the case record instead, and the caller
reads them from `GET /v1/cases/{case_id}`. What is left is only what can go wrong while a
request is still open — a bad upload, an unknown case id, a full queue.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from Detector.services.runner import TooBusy
from Detector.services.store import CaseExists, CaseNotFound

RETRY_AFTER_SECONDS = 5


class ErrorResponse(BaseModel):
    """The body of every error this API returns."""

    model_config = ConfigDict(extra='forbid')

    code: str = Field(description="Stable slug, e.g. 'invalid_case'. Switch on this.")
    detail: str = Field(description='What went wrong, in a sentence.')


class UploadRejected(ValueError):
    """An upload the API will not accept, carrying the status it should leave as.

    A `ValueError`, so anything that fails to catch it still becomes a 422 rather than a
    500 — but able to carry its own status, because 'too big' (413) and 'not a format we
    read' (415) tell a client what to change and a flat 422 does not.
    """

    def __init__(
        self,
        detail: str,
        *,
        status_code: int = status.HTTP_422_UNPROCESSABLE_CONTENT,
        code: str = 'invalid_case',
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code


def _error(status_code: int, code: str, detail: str, **headers: str) -> JSONResponse:
    """One error shape for the whole API."""
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(code=code, detail=detail).model_dump(),
        headers=headers or None,
    )


async def _case_not_found(request: Request, exc: Exception) -> JSONResponse:
    """Unknown id, or one that finished long enough ago to have expired."""
    return _error(status.HTTP_404_NOT_FOUND, 'case_not_found', str(exc))


async def _case_exists(request: Request, exc: Exception) -> JSONResponse:
    """Nothing is malformed; the id just collides. `DELETE` frees it."""
    return _error(status.HTTP_409_CONFLICT, 'case_exists', str(exc))


async def _invalid_case(request: Request, exc: Exception) -> JSONResponse:
    """A presentation the API will not take.

    The domain models raise a plain `ValueError` when one is malformed or too big, which
    is the right shape for a library — knowing that means 422 is this layer's job. An
    `UploadRejected` is a `ValueError` that brought a more specific status along, so the
    same handler answers both.
    """
    return _error(
        getattr(exc, 'status_code', status.HTTP_422_UNPROCESSABLE_CONTENT),
        getattr(exc, 'code', 'invalid_case'),
        str(exc),
    )


async def _too_busy(request: Request, exc: Exception) -> JSONResponse:
    """Shed the load rather than accept a case and sit on it."""
    return _error(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        'server_busy',
        f'the detector is at capacity: {exc}',
        **{'Retry-After': str(RETRY_AFTER_SECONDS)},
    )


def register_error_handlers(app: FastAPI) -> None:
    """Install the mapping on an app.

    Starlette resolves a handler by walking the exception's MRO, so the specific ones win
    over `ValueError` and registration order does not matter.
    """
    app.add_exception_handler(CaseNotFound, _case_not_found)
    app.add_exception_handler(CaseExists, _case_exists)
    app.add_exception_handler(UploadRejected, _invalid_case)
    app.add_exception_handler(ValueError, _invalid_case)
    app.add_exception_handler(TooBusy, _too_busy)
