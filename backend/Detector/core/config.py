"""Runtime configuration for the detector's model layer.

Everything here is overridable through the environment with a `DETECTOR_`
prefix, e.g. `DETECTOR_EXTRACTION_MODEL=anthropic:claude-sonnet-5`.
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

DEFAULT_MODEL = 'anthropic:claude-opus-5'
"""Used for every stage unless overridden. Change per stage, not globally, if you
want a cheaper model for the mechanical work (classification, extraction) while
keeping the reconciliation reasoning on the strongest model."""


class Settings(BaseSettings):
    """Model, prompt and budget configuration for the pipeline."""

    model_config = SettingsConfigDict(
        env_prefix='DETECTOR_',
        env_file='.env',
        env_file_encoding='utf-8',
        extra='ignore',
    )

    classifier_model: str = DEFAULT_MODEL
    extraction_model: str = DEFAULT_MODEL
    reconciliation_model: str = DEFAULT_MODEL

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
    """Add an Anthropic cache breakpoint after the system prompt. The extraction
    prompts are identical across every document in a case, so this pays off
    immediately on any presentation with more than one document."""

    request_limit: int = 8
    """Max model requests inside a single agent run (not per case)."""

    total_tokens_limit: int | None = None
    cost_limit: Decimal | None = Field(default=None)
    """Optional hard budget in USD for one agent run."""

    retries: int = 2
    """Output-validation and tool retries per agent run."""

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

    @property
    def uses_anthropic(self) -> bool:
        """Whether every stage is on the Anthropic provider.

        Anthropic-specific model settings (prompt caching) are only applied when
        this is true, so pointing a stage at another provider stays valid.
        """
        return all(
            model.startswith('anthropic:')
            for model in (self.classifier_model, self.extraction_model, self.reconciliation_model)
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process-wide settings singleton."""
    return Settings()
