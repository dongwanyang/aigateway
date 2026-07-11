"""Lazy app state accessor.

Avoids circular imports between admin_routes, openai_compat, routes, etc.
and main.py (which imports admin_routes in _mount_routes).

Usage (inside any route/handler function):

    from aigateway_api.app_state import get_state
    s = get_state()
    config_manager = getattr(s, "config_manager", None)
    key_store = getattr(s, "key_store", None)
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def get_state() -> object:
    """Return ``app.state`` from the running FastAPI application.

    Imports ``aigateway_api.main.app`` lazily and reads ``app.state`` fresh on
    every call. The import itself is cached by Python's module system, so this
    is cheap; we deliberately do NOT cache ``app.state`` because tests call
    ``create_app()`` per-case, and a stale cache would hand back the state of
    a torn-down app. Raises ``RuntimeError`` if the app has not been started
    (lifespan never ran).
    """
    try:
        from aigateway_api.main import app
    except ImportError as exc:
        raise RuntimeError(
            "Cannot import app.state — the FastAPI application has not been "
            f"started. Import error: {exc}"
        ) from exc
    return app.state  # type: ignore[no-any-return]
