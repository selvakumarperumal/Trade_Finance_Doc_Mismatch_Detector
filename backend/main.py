"""ASGI entry point: `uvicorn main:app`."""

from Detector.api.app import create_app

app = create_app()
