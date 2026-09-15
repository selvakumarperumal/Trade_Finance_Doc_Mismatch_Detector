"""Cross-cutting configuration.

`Settings` is every tunable this deployment has, and `get_settings()` is the
process-wide instance read from the environment once.
"""

from Detector.core.config import Settings, get_settings

__all__ = ['Settings', 'get_settings']
