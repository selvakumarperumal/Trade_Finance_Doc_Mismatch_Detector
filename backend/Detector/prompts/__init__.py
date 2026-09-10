"""The system prompts, loaded and validated from `Config/prompts.yaml`."""

from Detector.prompts.registry import (
    CLASSIFIER,
    RECONCILIATION,
    extractor,
    get_prompts,
    load_prompts,
)

__all__ = ['CLASSIFIER', 'RECONCILIATION', 'extractor', 'get_prompts', 'load_prompts']
