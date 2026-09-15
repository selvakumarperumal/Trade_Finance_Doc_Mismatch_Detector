"""Everything tunable about the detector, in one place.

Every field below is overridable from the environment with a `DETECTOR_` prefix, and a
`.env` file is read if there is one:

    DETECTOR_RECONCILIATION_MODEL=google:gemini-3.1-pro-preview
    DETECTOR_MAX_CONCURRENT_CASES=8
    DETECTOR_OCR_ENABLED=false

Credentials are not settings. The Google provider reads `GEMINI_API_KEY` or
`GOOGLE_API_KEY`, and boto3 resolves AWS credentials the usual way.

`core/README.md` explains why the limits and ceilings sit where they do.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_ai.settings import ThinkingLevel
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parents[2]
"""The `backend/` directory, so config paths do not depend on the working directory."""

MB = 1024 * 1024

FAST_MODEL = 'google:gemini-3.7-flash'
"""Classification and extraction are mechanical: locate the field, copy the value out."""

REASONING_MODEL = 'google:gemini-3.1-pro-preview'
"""Reconciliation is the compliance judgement — the one stage worth the stronger model."""


class Settings(BaseSettings):
    """Model, prompt and budget configuration for the pipeline."""

    model_config = SettingsConfigDict(
        env_prefix='DETECTOR_',
        env_file='.env',
        env_file_encoding='utf-8',
        extra='ignore',
    )

    # --- models ---------------------------------------------------------------
    # One model per stage. The first two look things up; the third is the judgement.

    classifier_model: str = FAST_MODEL
    extraction_model: str = FAST_MODEL
    reconciliation_model: str = REASONING_MODEL

    # Reasoning effort rises the same way, and so does room to write.
    classifier_thinking: ThinkingLevel = 'low'
    extraction_thinking: ThinkingLevel = 'medium'
    reconciliation_thinking: ThinkingLevel = 'high'

    classifier_max_tokens: int = 2_048
    extraction_max_tokens: int = 16_000
    reconciliation_max_tokens: int = 32_000

    # --- agent runs -----------------------------------------------------------

    prompts_path: Path = BACKEND_ROOT / 'Config' / 'prompts.yaml'  # loaded once at startup
    cache_instructions: bool = True  # cache breakpoint after the system prompt, where taken
    request_limit: int = 8  # model requests allowed inside one agent run
    retries: int = 2  # output-validation and tool retries per agent run
    total_tokens_limit: int | None = None
    cost_limit: Decimal | None = None  # hard USD budget for one agent run

    # --- what one case may not exceed -----------------------------------------

    max_documents_per_case: int = 25
    max_document_chars: int = 120_000  # longer documents are refused, never truncated
    max_document_bytes: int = 10 * MB  # Textract's synchronous API takes at most 10 MB
    max_case_bytes: int = 60 * MB  # every uploaded file together, enforced while reading
    max_document_pages: int = 30  # Textract bills per page, so a 500-page bundle is refused

    min_classification_confidence: float = 0.5
    """Below this a document is left unclassified rather than read under the wrong schema."""

    # --- how much runs at once ------------------------------------------------
    # Each bounds a different resource and none substitutes for another: requests to the
    # provider, calls to Textract, analyses in flight, submissions waiting for a slot.

    max_parallel_model_calls: int | None = 6  # provider requests in flight; `None` for no limit
    max_queued_model_calls: int | None = None  # calls that may wait for one of those slots
    max_parallel_ocr_calls: int = 4  # Textract is rate limited per account and region
    max_concurrent_cases: int = 4  # analyses at once; bounds memory, where the above bound requests
    max_queued_cases: int = 64  # submissions waiting before new ones are refused with a 503

    # --- OCR ------------------------------------------------------------------

    ocr_enabled: bool = True
    """Open a Textract client at startup.

    Off — or AWS simply not configured — still leaves the service running: `/health`
    reports `ocr_available: false` and documents must arrive with their text extracted.
    """

    textract_region: str | None = None  # unset falls back to AWS_REGION, then boto3's own search
    textract_max_attempts: int = 5  # botocore `adaptive` retries, which throttle the client too

    # --- holding a finished case ----------------------------------------------

    case_retention_seconds: int = 3_600
    """How long a finished case stays readable on `GET /v1/cases/{case_id}`.

    A record holds a whole presentation's extracted contents — counterparty names, bank
    details, amounts — so it is kept only long enough for its caller to collect it.
    """

    max_retained_cases: int = 500  # a duration alone bounds no memory; the oldest go first

    # --- api ------------------------------------------------------------------

    cors_origins: list[str] = Field(default_factory=list)
    """Browser origins allowed to call the API, e.g. `['http://localhost:5173']`.
    Empty means no CORS headers, which is right for a service behind a gateway — and for
    `frontend_dir`, where the page is served from this origin anyway."""

    frontend_dir: Path | None = None
    """Serve a static frontend from `/`, for a deployment that has no separate web server.

    Unset means the service is API-only. Set it and the page comes from the same origin
    as the API, which is why the shipped frontend needs no CORS configuration at all."""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process-wide settings, read from the environment once."""
    return Settings()
