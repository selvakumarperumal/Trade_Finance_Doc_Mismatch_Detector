"""Logfire wiring.

Three layers of the pipeline emit spans, and only the third needs code here:

1. Pydantic Graph builds a `run graph trade_finance_doc_mismatch` span and a
   child span per node, because `GraphBuilder(auto_instrument=True)` is the
   default. That is where the fan-out becomes visible: eight `extract_*` spans
   sitting side by side under one parent, with real start and end times.
2. Pydantic AI builds the model spans (request, tokens, tool calls) once
   `logfire.instrument_pydantic_ai()` has been called.
3. This module adds the case-level span that ties a trace to a business object.

None of it does anything until `configure_observability()` runs, so importing
the package stays free of side effects.
"""

from __future__ import annotations

from collections.abc import Generator, Mapping
from contextlib import contextmanager
from typing import Any, Protocol

import logfire

from Detector.core.config import Settings, get_settings

_configured = False


class Span(Protocol):
    """The subset of the Logfire span API this package uses."""

    def set_attribute(self, key: str, value: Any) -> None: ...

    def set_attributes(self, attributes: Mapping[str, Any]) -> None: ...


class _NoopSpan:
    """Stands in for a span when Logfire is switched off.

    Cheaper than a disabled real span, and it keeps `logfire.span()` from
    warning that Logfire was never configured.
    """

    def set_attribute(self, key: str, value: Any) -> None:
        return None

    def set_attributes(self, attributes: Mapping[str, Any]) -> None:
        return None


def configure_observability(settings: Settings | None = None) -> bool:
    """Configure Logfire and instrument Pydantic AI. Safe to call more than once.

    Call this once at startup, before the first agent run — from the FastAPI
    lifespan handler, say — so that every later span lands in the same trace
    tree. `get_pipeline()` calls it as a fallback if you don't.

    Returns:
        Whether Logfire is now active.
    """
    global _configured
    if _configured:
        return True

    settings = settings or get_settings()
    if not settings.logfire_enabled:
        return False

    logfire.configure(
        service_name=settings.logfire_service_name,
        service_version=settings.logfire_service_version,
        environment=settings.logfire_environment,
        send_to_logfire=settings.logfire_send_to_logfire,
        console=logfire.ConsoleOptions() if settings.logfire_console else False,
    )
    logfire.instrument_pydantic_ai(
        include_content=settings.logfire_include_content,
        include_binary_content=False,
    )
    _configured = True
    return True


def is_configured() -> bool:
    """Whether `configure_observability()` has taken effect."""
    return _configured


@contextmanager
def span(name: str, /, **attributes: Any) -> Generator[Span]:
    """Open a span, or hand back a no-op if Logfire is off.

    `name` is a Logfire message template, so `'analyse case {case_id}'` with
    `case_id=...` groups every case under one span name while still showing the
    individual id on each trace.
    """
    if not _configured:
        yield _NoopSpan()
        return
    with logfire.span(name, **attributes) as logfire_span:
        yield logfire_span
