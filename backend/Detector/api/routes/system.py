"""Is the service up, and can it read scans?"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from Detector.api.dependencies import RunnerDep

router = APIRouter(tags=['system'])


class HealthResponse(BaseModel):
    """Liveness, plus the one fact worth knowing before sending a case."""

    status: Literal['ok'] = 'ok'
    ocr_available: bool = Field(
        description='False means documents must arrive with their text already extracted.'
    )
    cases_in_flight: int


@router.get('/health', summary='Liveness check')
async def health(runner: RunnerDep) -> HealthResponse:
    """Whether the service is up, and whether it can read scans.

    `ocr_available` is false when AWS is not configured or `DETECTOR_OCR_ENABLED` is off.
    Presentations that arrive as text are still analysed, so this is a capability flag,
    not a failure — which is why the status stays `ok`.
    """
    return HealthResponse(
        ocr_available=runner.pipeline.ocr_available,
        cases_in_flight=runner.in_flight,
    )
