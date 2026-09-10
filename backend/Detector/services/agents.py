"""The Pydantic AI agents: one classifier, one extractor per document family, one reconciler.

Agents are built once and reused for the life of the process. They hold no
per-case state, so a single registry is safe to share across concurrent runs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.concurrency import ConcurrencyLimit, ConcurrencyLimiter
from pydantic_ai.models.anthropic import AnthropicModelSettings
from pydantic_ai.settings import ModelSettings, ThinkingLevel

from Detector.core.config import Settings, get_settings
from Detector.models.documents import Classification
from Detector.models.enums import DocumentType
from Detector.models.extractions import EXTRACTION_PAYLOAD_TYPES
from Detector.models.reconciliation import ReconciliationReport
from Detector.prompts import registry
from Detector.prompts.registry import get_prompts
from Detector.services.tools import RECONCILIATION_TOOLS

type ExtractionAgent = Agent[None, Any]
"""An extractor's output type varies by document family, so it is erased here."""


def build_limiter(settings: Settings) -> ConcurrencyLimiter | None:
    """One concurrency ceiling shared by every agent in the process.

    The graph fans out over documents, so a 20-document presentation would otherwise
    open 20 simultaneous model connections and collect 429s. The classifier, the eight
    extractors and the reconciler all draw on the same provider rate limit, so they
    share one limiter rather than getting one each.

    `max_queued_model_calls` is the backpressure valve: with it set, a call that would
    have to wait behind that many others fails fast with `ConcurrencyLimitExceeded`
    instead of leaving a caller holding an HTTP connection open indefinitely.
    """
    if settings.max_parallel_model_calls is None:
        return None
    return ConcurrencyLimiter.from_limit(
        ConcurrencyLimit(
            max_running=settings.max_parallel_model_calls,
            max_queued=settings.max_queued_model_calls,
        ),
        name='detector-model-calls',
    )


def _model_settings(
    *,
    model: str,
    thinking: ThinkingLevel,
    max_tokens: int,
    cache_instructions: bool,
) -> ModelSettings:
    """Build one stage's model settings.

    `max_tokens` and `thinking` are provider-neutral: pydantic-ai maps the thinking
    level onto whatever the provider calls it. Prompt caching is not neutral, so it is
    applied by provider — Anthropic wants an explicit breakpoint after the system
    prompt, while Gemini, the default here, caches long prefixes without being asked.
    Any stage may be pointed at any provider, so the check is per model, not per process.
    """
    base: ModelSettings = {'max_tokens': max_tokens, 'thinking': thinking}
    if model.startswith('anthropic:') and cache_instructions:
        # The system prompt is byte-identical across every document in a case, so a
        # breakpoint after it turns the second and later documents into cache reads.
        return AnthropicModelSettings(**base, anthropic_cache_instructions=True)
    return base


@dataclass(frozen=True, slots=True)
class AgentRegistry:
    """Every agent the pipeline needs, built and ready."""

    classifier: Agent[None, Classification]
    """Decides which document family a piece of text belongs to."""

    extractors: Mapping[DocumentType, ExtractionAgent]
    """One per family; each returns that family's typed payload."""

    reconciler: Agent[None, ReconciliationReport]
    """Cross-checks the extracted documents and issues the verdict."""

    def extractor(self, document_type: DocumentType) -> ExtractionAgent:
        """The extraction agent for one document family."""
        try:
            return self.extractors[document_type]
        except KeyError as exc:
            raise KeyError(f'no extraction agent for document type {document_type!r}') from exc


def build_agents(settings: Settings, prompts: Mapping[str, str]) -> AgentRegistry:
    """Construct the agent registry from settings and the prompt registry."""
    limiter = build_limiter(settings)

    classifier = Agent(
        settings.classifier_model,
        name='document_classifier',
        output_type=Classification,
        instructions=prompts[registry.CLASSIFIER],
        retries=settings.retries,
        max_concurrency=limiter,
        model_settings=_model_settings(
            model=settings.classifier_model,
            thinking=settings.classifier_thinking,
            max_tokens=settings.classifier_max_tokens,
            cache_instructions=settings.cache_instructions,
        ),
    )

    extraction_settings = _model_settings(
        model=settings.extraction_model,
        thinking=settings.extraction_thinking,
        max_tokens=settings.extraction_max_tokens,
        cache_instructions=settings.cache_instructions,
    )
    extractors: dict[DocumentType, ExtractionAgent] = {
        document_type: Agent(
            settings.extraction_model,
            name=f'{document_type.value}_extractor',
            output_type=payload_type,
            instructions=prompts[registry.extractor(document_type)],
            retries=settings.retries,
            max_concurrency=limiter,
            model_settings=extraction_settings,
        )
        for document_type, payload_type in EXTRACTION_PAYLOAD_TYPES.items()
    }

    reconciler = Agent(
        settings.reconciliation_model,
        name='reconciliation_engine',
        output_type=ReconciliationReport,
        instructions=prompts[registry.RECONCILIATION],
        tools=RECONCILIATION_TOOLS,
        retries=settings.retries,
        max_concurrency=limiter,
        model_settings=_model_settings(
            model=settings.reconciliation_model,
            thinking=settings.reconciliation_thinking,
            max_tokens=settings.reconciliation_max_tokens,
            cache_instructions=settings.cache_instructions,
        ),
    )

    return AgentRegistry(classifier=classifier, extractors=extractors, reconciler=reconciler)


@lru_cache(maxsize=1)
def get_agents() -> AgentRegistry:
    """The process-wide agent registry."""
    return build_agents(get_settings(), get_prompts())
