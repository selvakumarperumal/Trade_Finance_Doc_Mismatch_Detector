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
from pydantic_ai.models.anthropic import AnthropicModelSettings
from pydantic_ai.settings import ModelSettings, ThinkingLevel

from Detector.core.config import Settings, get_settings
from Detector.models.documents import Classification
from Detector.models.enums import DocumentType
from Detector.models.extractions import EXTRACTION_PAYLOAD_TYPES
from Detector.models.reconciliation import ReconciliationReport
from Detector.prompts.registry import PromptRegistry, get_prompts
from Detector.services.tools import RECONCILIATION_TOOLS

type ExtractionAgent = Agent[None, Any]
"""An extractor's output type varies by document family, so it is erased here."""


def _model_settings(
    *,
    model: str,
    thinking: ThinkingLevel,
    max_tokens: int,
    cache_instructions: bool,
) -> ModelSettings:
    """Build the per-stage model settings, adding Anthropic extras only on Anthropic models."""
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


def build_agents(settings: Settings, prompts: PromptRegistry) -> AgentRegistry:
    """Construct the agent registry from settings and the prompt registry."""
    classifier = Agent(
        settings.classifier_model,
        name='document_classifier',
        output_type=Classification,
        instructions=prompts.classifier,
        retries=settings.retries,
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
            instructions=prompts.extractor(document_type),
            retries=settings.retries,
            model_settings=extraction_settings,
        )
        for document_type, payload_type in EXTRACTION_PAYLOAD_TYPES.items()
    }

    reconciler = Agent(
        settings.reconciliation_model,
        name='reconciliation_engine',
        output_type=ReconciliationReport,
        instructions=prompts.reconciliation,
        tools=RECONCILIATION_TOOLS,
        retries=settings.retries,
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
