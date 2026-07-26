"""Access state from the FastAPI application that is actually running.

``uvicorn ...:create_app --factory`` creates an application instance that is
different from the module-level ``aigateway_api.main.app`` object.  Importing
that global object here therefore returned state from an app whose lifespan
had never run.

The lifespan wrapper now registers the active application explicitly.  Route
handlers may also pass their ``Request`` to avoid any process-global lookup.
"""

from __future__ import annotations

import logging
from threading import RLock
from typing import Any

logger = logging.getLogger(__name__)

_active_apps: list[Any] = []
_active_apps_lock = RLock()


def activate_app(app: Any) -> None:
    """Register an application for the duration of its lifespan.

    A list is used instead of a single assignment so nested TestClient
    lifespans restore the previously active app when the inner client exits.
    """
    with _active_apps_lock:
        _active_apps[:] = [
            active_app for active_app in _active_apps if active_app is not app
        ]
        _active_apps.append(app)


def deactivate_app(app: Any) -> None:
    """Remove a previously registered application, if present."""
    with _active_apps_lock:
        _active_apps[:] = [
            active_app for active_app in _active_apps if active_app is not app
        ]


def get_state(request: Any | None = None) -> object:
    """Return state from the request app or the active lifespan app.

    Args:
        request: Optional FastAPI/Starlette request.  Passing it is preferred
            in route handlers because it identifies the app unambiguously.

    Raises:
        RuntimeError: If called without a request while no application
            lifespan is active.
    """
    if request is not None:
        request_app = getattr(request, "app", None)
        if request_app is None:
            raise RuntimeError("Cannot access app.state: request has no app")
        return request_app.state  # type: ignore[no-any-return]

    with _active_apps_lock:
        active_app = _active_apps[-1] if _active_apps else None

    if active_app is None:
        raise RuntimeError(
            "Cannot access app.state: no FastAPI application lifespan is active"
        )
    return active_app.state  # type: ignore[no-any-return]
