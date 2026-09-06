"""Cross-cutting configuration and observability."""

from Detector.core.config import Settings, get_settings
from Detector.core.observability import configure_observability, is_configured, span

__all__ = ['Settings', 'configure_observability', 'get_settings', 'is_configured', 'span']
