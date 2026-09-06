"""System prompts, loaded from `Config/prompts.yaml`."""

from Detector.prompts.registry import PromptRegistry, get_prompts

__all__ = ['PromptRegistry', 'get_prompts']
