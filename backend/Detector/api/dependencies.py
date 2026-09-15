"""What routes ask for, and where it comes from.

Two dependencies, and one object leads to everything: the runner. It owns both the
pipeline cases run through and the place their records are kept, and exposes `submit`,
`get`, `watch` and `discard`, so a route never has to reach past it. Both dependencies
are built in the lifespan handler and live on `app.state`, so the wiring is stated here
in one place and a test can replace either with `app.dependency_overrides`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from Detector.core.config import Settings, get_settings
from Detector.services.runner import CaseRunner


def get_runner(request: Request) -> CaseRunner:
    """The runner cases are submitted to, or a 503 while the app is still starting.

    Only the HTTP routes go through here. The Socket.IO handlers in `api/events.py` read
    the same `app.state.runner` themselves, because they are not FastAPI endpoints and
    have no dependency injection to hang this on.
    """
    runner = getattr(request.app.state, 'runner', None)
    if not isinstance(runner, CaseRunner):  # pragma: no cover - only if the lifespan did not run
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='the detector is still starting up',
        )
    return runner


def get_app_settings(request: Request) -> Settings:
    """The settings *this app* was built with.

    Not `get_settings()` directly: that is the process-wide singleton read from the
    environment, and an app created with an explicit `Settings` would then validate
    uploads against limits it was never given.
    """
    settings = getattr(request.app.state, 'settings', None)
    return settings if isinstance(settings, Settings) else get_settings()


RunnerDep = Annotated[CaseRunner, Depends(get_runner)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
