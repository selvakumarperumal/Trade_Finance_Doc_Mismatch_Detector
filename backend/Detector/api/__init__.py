"""The HTTP layer: request and response shapes, routes, and the app factory."""

from Detector.api.app import create_app, lifespan

__all__ = ['create_app', 'lifespan']
