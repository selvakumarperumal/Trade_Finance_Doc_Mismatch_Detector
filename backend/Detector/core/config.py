"""Runtime configuration for the detector's model layer.

Everything here is overridable through the environment with a `DETECTOR_`
prefix, e.g. `DETECTOR_EXTRACTION_MODEL=google:gemini-3.7-flash`.

The Google provider reads `GEMINI_API_KEY` or `GOOGLE_API_KEY` from the environment.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_ai.settings import ThinkingLevel
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parents[2]
"""The `backend/` directory, so config paths do not depend on the working directory."""

FAST_MODEL = 'google:gemini-3.7-flash'
"""Classification and extraction are mechanical: locate the field, copy the value out."""

REASONING_MODEL = 'google:gemini-3.1-pro-preview'
"""Reconciliation is the compliance judgement — the one stage worth the stronger model.

Deciding whether a bank would refuse a presentation is where the reasoning goes; the
stages before it are looking things up. Splitting the two is most of the reason a
twenty-document case is affordable."""


class Settings(BaseSettings):
    """Model, prompt and budget configuration for the pipeline."""

    model_config = SettingsConfigDict(
        env_prefix='DETECTOR_',
        env_file='.env',
        env_file_encoding='utf-8',
        extra='ignore',
    )

    classifier_model: str = FAST_MODEL
    extraction_model: str = FAST_MODEL
    reconciliation_model: str = REASONING_MODEL

    classifier_thinking: ThinkingLevel = 'low'
    """Classification is pattern matching; it does not need deep reasoning."""

    extraction_thinking: ThinkingLevel = 'medium'
    """Extraction has to cope with messy OCR text and unlabelled fields."""

    reconciliation_thinking: ThinkingLevel = 'high'
    """The compliance judgement is the part worth spending reasoning on."""

    classifier_max_tokens: int = 2_048
    extraction_max_tokens: int = 16_000
    reconciliation_max_tokens: int = 32_000

    prompts_path: Path = BACKEND_ROOT / 'Config' / 'prompts.yaml'
    """Where the system prompts live. Loaded once at startup."""

    max_document_chars: int = 120_000
    """Documents longer than this are rejected rather than silently truncated."""

    max_documents_per_case: int = 25
    """Guard against a single presentation fanning out into an unbounded number of runs."""

    min_classification_confidence: float = 0.5
    """Below this the document is treated as unclassified rather than sent to an
    extractor that would read it under the wrong schema."""

    cache_instructions: bool = True
    """Add a cache breakpoint after the system prompt, where the provider takes one.

    The extraction prompts are identical across every document in a case, so this pays
    off immediately on any presentation with more than one document. Only Anthropic
    models take an explicit breakpoint — Gemini caches long prefixes on its own — so on
    the default Google models this setting has nothing to apply and is simply inert.
    """

    request_limit: int = 8
    """Max model requests inside a single agent run (not per case)."""

    total_tokens_limit: int | None = None
    cost_limit: Decimal | None = Field(default=None)
    """Optional hard budget in USD for one agent run."""

    retries: int = 2
    """Output-validation and tool retries per agent run."""

    # --- OCR -----------------------------------------------------------------

    ocr_enabled: bool = True
    """Open a Textract client at startup.

    Off means documents must arrive with their text already extracted. It is also the
    automatic fallback when AWS is not configured: the service starts without OCR and
    says so on `/health`, rather than refusing to start at all."""

    textract_region: str | None = None
    """AWS region for Textract. `None` lets boto3 resolve it the usual way, from
    `AWS_REGION`, `AWS_DEFAULT_REGION` or the active profile."""

    textract_max_attempts: int = 5
    """Retry budget per Textract call. The client runs botocore's `adaptive` mode, which
    backs off *and* throttles the client itself once Textract starts refusing — the right
    behaviour behind a fan-out, where a naive retry storm makes the throttling worse."""

    max_parallel_ocr_calls: int = 4
    """Textract calls in flight across the whole process.

    `DetectDocumentText` is rate limited per account and region, so a 25-document case
    fanning out unbounded would collect `ProvisionedThroughputExceededException` instead
    of text. Keep this under the account's transactions-per-second quota."""

    max_document_bytes: int = 10 * 1024 * 1024
    """Largest file the OCR step will accept for one document.

    Textract's synchronous API takes at most 10 MB inline, and a multi-page PDF is split
    into one call per page, so this bounds the whole file rather than a single call."""

    max_case_bytes: int = 60 * 1024 * 1024
    """Total upload size for one submission, across every file.

    `max_document_bytes` alone bounds nothing useful: twenty-five files just under the
    per-file limit is a quarter of a gigabyte held in memory for the life of the run.
    This is the cap the upload route enforces while it reads, so an oversized batch is
    refused part-way through rather than after it has all landed."""

    max_document_pages: int = 30
    """Pages the OCR step will read from one document.

    Textract bills per page and each page is a separate call, so a mistakenly uploaded
    500-page bundle is refused rather than quietly costing 500 reads."""

    # --- parallelism ---------------------------------------------------------

    max_parallel_model_calls: int | None = 6
    """How many model calls the whole case may have in flight at once.

    The graph fans out over documents, so a 20-document presentation would
    otherwise open 20 simultaneous connections and collect 429s. One limiter is
    shared by the classifier, every extractor and the reconciler, because they
    all draw on the same provider rate limit. `None` removes the ceiling."""

    max_queued_model_calls: int | None = None
    """How many calls may wait for a slot before the run is rejected outright.

    `None` queues without limit, which is right when a case is a background job.
    Set it when a caller is waiting on a response and would rather fail fast."""

    # --- jobs ----------------------------------------------------------------

    max_concurrent_cases: int = 4
    """Cases the process will analyse at once.

    A submission returns immediately with a case id and the run continues in the
    background, so without a ceiling a burst of submissions would all start at once and
    contend for the same provider rate limit. The model-call limiter bounds requests;
    this bounds runs, which is what keeps memory (uploaded bytes, per-case state) flat."""

    max_queued_cases: int = 64
    """Submissions that may wait for a slot before new ones are refused with 503.

    Queueing without limit turns an overload into an unbounded backlog where every
    caller waits and none of them are told. This is the point where the service says no."""

    case_retention_seconds: int = 3_600
    """How long a finished case stays readable on `GET /v1/cases/{case_id}`.

    The result holds the full extracted contents of a presentation — counterparty names,
    bank details, amounts — so it is held only long enough for the caller that submitted
    it to collect it, then dropped. Cases are kept in memory, so this also bounds how
    much a busy process accumulates."""

    max_retained_cases: int = 500
    """Hard ceiling on retained cases, whatever the retention window says.

    Retention is a duration and duration alone does not bound memory under load; the
    oldest finished cases are evicted first once this many are held."""

    # --- api -----------------------------------------------------------------

    cors_origins: list[str] = Field(default_factory=list)
    """Browser origins allowed to call the API, e.g. `["http://localhost:5173"]`.
    Empty means no CORS headers, which is right for a service behind a gateway."""

    # --- observability -------------------------------------------------------

    logfire_enabled: bool = True
    """Configure Logfire on startup. Off means no spans and no exporter."""

    logfire_service_name: str = 'trade-finance-detector'
    logfire_service_version: str | None = None
    logfire_environment: str | None = None
    """e.g. 'production' or 'staging'; shows up as a filter in the Logfire UI."""

    logfire_send_to_logfire: bool | Literal['if-token-present'] = 'if-token-present'
    """Default means a missing `LOGFIRE_TOKEN` degrades to local-only spans
    rather than crashing the service at startup."""

    logfire_console: bool = False
    """Print spans to stdout. Useful in development, noisy in a container."""

    logfire_include_content: bool = False
    """Whether prompts and completions are attached to spans.

    Off by default on purpose: the prompts here contain the full text of letters
    of credit and invoices, which means counterparty names, bank details and
    amounts. Turn it on deliberately, and only where the telemetry backend is
    inside the same trust boundary as the documents."""

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process-wide settings singleton."""
    return Settings()
