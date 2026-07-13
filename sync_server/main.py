"""ASGI entry point kept separate so importing the app factory has no side effects."""

from .app import create_app


app = create_app()
