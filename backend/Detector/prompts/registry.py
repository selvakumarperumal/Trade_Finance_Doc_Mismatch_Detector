"""Loads the system prompts from `Config/prompts.yaml` and hands them to the agents.

Prompts live in YAML so an ops or compliance reviewer can change the wording of a
check without touching Python. The registry validates on load, so a typo in the
YAML fails at startup rather than on the first request.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Final

import yaml

from Detector.core.config import get_settings
from Detector.models.enums import DocumentType

CLASSIFIER_PROMPT: Final = 'classifier'
RECONCILIATION_PROMPT: Final = 'reconciliation_engine'

EXTRACTOR_PROMPTS: Final[Mapping[DocumentType, str]] = {
    DocumentType.LETTER_OF_CREDIT: 'lc_extractor',
    DocumentType.COMMERCIAL_INVOICE: 'invoice_extractor',
    DocumentType.BILL_OF_LADING: 'bol_extractor',
    DocumentType.PACKING_LIST: 'packing_list_extractor',
    DocumentType.CERTIFICATE_OF_ORIGIN: 'certificate_of_origin_extractor',
    DocumentType.INSURANCE_CERTIFICATE: 'insurance_certificate_extractor',
    DocumentType.BILL_OF_EXCHANGE: 'bill_of_exchange_extractor',
    DocumentType.INSPECTION_CERTIFICATE: 'inspection_certificate_extractor',
}
"""Which `system_prompts` key drives each document family's extractor."""

REQUIRED_PROMPTS: Final[frozenset[str]] = frozenset(
    {CLASSIFIER_PROMPT, RECONCILIATION_PROMPT, *EXTRACTOR_PROMPTS.values()}
)


class PromptRegistry:
    """The system prompts, keyed by the names used in `Config/prompts.yaml`."""

    __slots__ = ('_prompts',)

    def __init__(self, prompts: Mapping[str, str]) -> None:
        missing = REQUIRED_PROMPTS - prompts.keys()
        if missing:
            raise ValueError(f'prompts file is missing required system prompts: {sorted(missing)}')
        blank = sorted(name for name in REQUIRED_PROMPTS if not prompts[name].strip())
        if blank:
            raise ValueError(f'prompts file has empty system prompts: {blank}')
        self._prompts = dict(prompts)

    @classmethod
    def from_yaml(cls, path: Path) -> PromptRegistry:
        """Read the registry from a prompts YAML file."""
        try:
            raw = yaml.safe_load(path.read_text(encoding='utf-8'))
        except FileNotFoundError as exc:
            raise FileNotFoundError(f'prompts file not found at {path}') from exc

        if not isinstance(raw, dict):
            raise ValueError(f'{path} must contain a mapping at the top level')

        prompts = raw.get('system_prompts')
        if not isinstance(prompts, dict):
            raise ValueError(f"{path} must contain a 'system_prompts' mapping")

        non_strings = sorted(key for key, value in prompts.items() if not isinstance(value, str))
        if non_strings:
            raise ValueError(f'{path}: system prompts must be strings, got non-strings for {non_strings}')

        return cls(prompts)

    def get(self, name: str) -> str:
        """The prompt registered under `name`, stripped of trailing whitespace."""
        try:
            return self._prompts[name].strip()
        except KeyError as exc:
            raise KeyError(f'unknown system prompt {name!r}; known prompts: {sorted(self._prompts)}') from exc

    @property
    def classifier(self) -> str:
        """The document classification prompt."""
        return self.get(CLASSIFIER_PROMPT)

    @property
    def reconciliation(self) -> str:
        """The documentary compliance prompt."""
        return self.get(RECONCILIATION_PROMPT)

    def extractor(self, document_type: DocumentType) -> str:
        """The extraction prompt for one document family."""
        try:
            key = EXTRACTOR_PROMPTS[document_type]
        except KeyError as exc:
            raise KeyError(f'no extractor prompt for document type {document_type!r}') from exc
        return self.get(key)


@lru_cache(maxsize=1)
def get_prompts() -> PromptRegistry:
    """The process-wide prompt registry, loaded from the configured path."""
    return PromptRegistry.from_yaml(get_settings().prompts_path)
