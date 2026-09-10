"""HTTP and websocket routes."""

from Detector.api.routes.cases import router as cases_router
from Detector.api.routes.system import router as system_router

__all__ = ['cases_router', 'system_router']
