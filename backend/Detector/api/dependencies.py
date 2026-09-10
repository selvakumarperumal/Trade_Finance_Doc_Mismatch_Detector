"""What routes ask for, and where it comes from.

One dependency, because one object leads to the rest: the runner owns the store cases
are collected from and the pipeline they run through. It is built once in the lifespan
handler and lives on `app.state`, so the wiring is stated here and a test can replace it
with `app.dependency_overrides`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, status
from starlette.requests import HTTPConnection

from Detector.core.config import Settings, get_settings
from Detector.services.runner import CaseRunner


def get_runner(connection: HTTPConnection) -> CaseRunner:
    """The runner cases are submitted to, or a 503 while the app is still starting.

    `HTTPConnection` is the base of both `Request` and `WebSocket`, so this serves the
    routes and the progress stream alike.
    """
    runner = getattr(connection.app.state, 'runner', None)
    if not isinstance(runner, CaseRunner):  # pragma: no cover - only if the lifespan did not run
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='the detector is still starting up',
        )
    return runner


def get_app_settings(connection: HTTPConnection) -> Settings:
    """The settings *this app* was built with.

    Not `get_settings()` directly: that is the process-wide singleton read from the
    environment, and an app created with an explicit `Settings` would then validate
    uploads against limits it was never given.
    """
    settings = getattr(connection.app.state, 'settings', None)
    return settings if isinstance(settings, Settings) else get_settings()


RunnerDep = Annotated[CaseRunner, Depends(get_runner)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
