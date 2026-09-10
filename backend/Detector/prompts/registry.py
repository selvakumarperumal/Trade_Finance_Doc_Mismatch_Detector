"""Loads the system prompts from `Config/prompts.yaml`.

Prompts live in YAML so an ops or compliance reviewer can reword a check without
touching Python, and are validated on load, so a typo fails at startup rather than on
the first request.

Every key is derivable from the document types — `classifier`, `reconciliation`, and
`<document_type>_extractor` — so there is no table here mapping one naming scheme onto
another. Adding a document family means adding its prompt under the matching name.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Final

import yaml

from Detector.core.config import get_settings
from Detector.models.enums import DocumentType

CLASSIFIER: Final = 'classifier'
RECONCILIATION: Final = 'reconciliation'


def extractor(document_type: DocumentType) -> str:
    """The prompt name for one document family's extractor."""
    return f'{document_type.value}_extractor'


REQUIRED: Final[frozenset[str]] = frozenset(
    {CLASSIFIER, RECONCILIATION}
    | {extractor(t) for t in DocumentType if t is not DocumentType.UNKNOWN}
)
"""Every prompt the agents need. Anything else in the file is ignored."""


def load_prompts(path: Path) -> dict[str, str]:
    """Read and validate the prompts file.

    Raises:
        FileNotFoundError: no file at `path`.
        ValueError: the file is not shaped like a prompts file, or a required prompt is
            missing or blank.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding='utf-8'))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f'prompts file not found at {path}') from exc

    prompts = raw.get('system_prompts') if isinstance(raw, dict) else None
    if not isinstance(prompts, dict):
        # ValueError, not TypeError: the file's *contents* are wrong, which is a
        # configuration problem, not a caller passing the wrong kind of argument.
        raise ValueError(f"{path} must contain a 'system_prompts' mapping")  # noqa: TRY004

    unusable = sorted(
        name
        for name in REQUIRED
        if not isinstance(prompts.get(name), str) or not prompts[name].strip()
    )
    if unusable:
        raise ValueError(f'{path} is missing or has blank system prompts: {unusable}')

    return {name: prompts[name].strip() for name in REQUIRED}


@lru_cache(maxsize=1)
def get_prompts() -> Mapping[str, str]:
    """The process-wide prompts, loaded from the configured path."""
    return load_prompts(get_settings().prompts_path)
