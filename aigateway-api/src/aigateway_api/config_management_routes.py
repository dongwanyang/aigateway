"""Transactional replacements for legacy partial configuration endpoints."""
from __future__ import annotations

import copy
import re
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from .auth_middleware import authenticate_admin
from .config_security import ConfigCommit, read_versioned_yaml_config, transactional_replace_config
from .security_routes import (
    _commit_revision,
    _config_error,
    _expected_revision,
    _json_object,
    _manager,
    _versioned_response,
)

router = APIRouter()

_GENERATION_PLUGIN_CONFIG_PATH: dict[str, list[str]] = {
    "ai_director": ["ai_director", "enabled"],
    "intent_evaluator": ["model_router", "enabled"],
    "gen_model_router": ["model_router", "enabled"],
    "token_compressor": ["token_compressor", "enabled"],
    "draft_generator": ["draft_workflow", "enabled"],
    "cost_tracker": ["cost_tracking", "enabled"],
}
_PLUGIN_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_DEBUG_SCALAR_KEYS = {"frontend", "entry", "cache", "bridge"}


def _validation_error(message: str, *, status_code: int = 400) -> HTTPException:
    return HTTPException(
        status_code,
        detail={"error": {"code": "validation_error", "message": message}},
    )


def _reject_unknown(body: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise _validation_error("Unknown fields: " + ", ".join(unknown))


def _require_bool(body: dict[str, Any], key: str) -> bool:
    value = body.get(key)
    if type(value) is not bool:
        raise _validation_error(f"{key} must be a boolean")
    return value


def _optional_revision(request: Request, current_revision: str) -> str:
    raw = (
        request.headers.get("if-match", "").strip()
        or request.query_params.get("revision", "").strip()
    )
    return _expected_revision(request) if raw else current_revision


def _transactional_mutation(
    request: Request,
    manager: Any,
    mutate: Callable[[dict[str, Any]], None],
    *,
    runtime_manager: Any | None = None,
) -> ConfigCommit:
    candidate, current_revision = read_versioned_yaml_config(manager.config_path)
    mutate(candidate)
    return transactional_replace_config(
        manager.config_path,
        candidate,
        runtime_manager or manager,
        expected_revision=_optional_revision(request, current_revision),
    )


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _validation_error(f"{path} must be an object", status_code=422)
    return value


def _sequence(value: Any, path: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _validation_error(f"{path} must be an array", status_code=422)
    return value


@router.put("/plugins-config")
async def update_plugins_config_transactional(
    request: Request,
    _auth: dict[str, Any] = Depends(authenticate_admin),
):
    manager = _manager(request)
    try:
        body = await _json_object(request)
        _reject_unknown(body, {"name", "enabled"})
        name = body.get("name")
        if not isinstance(name, str) or not _PLUGIN_NAME_RE.fullmatch(name):
            raise _validation_error("name must be a valid plugin name")
        enabled = _require_bool(body, "enabled")
        generation_path = _GENERATION_PLUGIN_CONFIG_PATH.get(name)

        def mutate(candidate: dict[str, Any]) -> None:
            plugins = _sequence(candidate.get("plugins"), "plugins")
            updated = False
            for plugin in plugins:
                if isinstance(plugin, dict) and plugin.get("name") == name:
                    plugin["enabled"] = enabled
                    updated = True
                    break
            if not updated and generation_path:
                generation = _mapping(
                    candidate.setdefault("generation_optimization", {}),
                    "generation_optimization",
                )
                generation["enabled"] = True
                current = generation
                for index, segment in enumerate(generation_path[:-1], start=1):
                    current = _mapping(
                        current.setdefault(segment, {}),
                        "generation_optimization." + ".".join(generation_path[:index]),
                    )
                current[generation_path[-1]] = enabled
                updated = True
            if not updated:
                raise HTTPException(
                    404,
                    detail={
                        "error": {
                            "code": "not_found",
                            "message": f"Plugin '{name}' not found",
                        }
                    },
                )

        commit = _transactional_mutation(request, manager, mutate)
    except Exception as exc:
        raise _config_error(exc) from exc
    return _versioned_response(
        {"name": name, "enabled": enabled},
        _commit_revision(commit, manager.config_path),
    )


@router.post("/plugins/{plugin_name}/debug")
async def set_plugin_debug_transactional(
    plugin_name: str,
    request: Request,
    _auth: dict[str, Any] = Depends(authenticate_admin),
):
    manager = _manager(request)
    try:
        if not _PLUGIN_NAME_RE.fullmatch(plugin_name):
            raise _validation_error("plugin_name must be a valid plugin name")
        body = await _json_object(request)
        _reject_unknown(body, {"enabled"})
        enabled = _require_bool(body, "enabled")

        def mutate(candidate: dict[str, Any]) -> None:
            debug = _mapping(candidate.setdefault("debug", {}), "debug")
            plugins = _mapping(debug.setdefault("plugins", {}), "debug.plugins")
            per_plugin = _mapping(
                plugins.setdefault("per_plugin", {}),
                "debug.plugins.per_plugin",
            )
            per_plugin[plugin_name] = enabled

        commit = _transactional_mutation(request, manager, mutate)
    except Exception as exc:
        raise _config_error(exc) from exc
    return _versioned_response(
        {"plugin": plugin_name, "debug": enabled},
        _commit_revision(commit, manager.config_path),
    )


def _merge_debug_section(current: Any, submitted: Any) -> dict[str, Any]:
    current_debug = copy.deepcopy(_mapping(current, "debug"))
    submitted_debug = _mapping(submitted, "debug")
    _reject_unknown(
        submitted_debug,
        _DEBUG_SCALAR_KEYS | {"plugins_enabled", "plugins"},
    )

    for key in _DEBUG_SCALAR_KEYS:
        if key in submitted_debug:
            current_debug[key] = _require_bool(submitted_debug, key)

    current_plugins = _mapping(
        current_debug.setdefault("plugins", {}),
        "debug.plugins",
    )
    if "plugins" in submitted_debug:
        submitted_plugins = _mapping(
            submitted_debug["plugins"],
            "debug.plugins",
        )
        _reject_unknown(submitted_plugins, {"enabled", "per_plugin"})
        if "enabled" in submitted_plugins:
            current_plugins["enabled"] = _require_bool(
                submitted_plugins,
                "enabled",
            )
        if "per_plugin" in submitted_plugins:
            submitted_per_plugin = _mapping(
                submitted_plugins["per_plugin"],
                "debug.plugins.per_plugin",
            )
            current_per_plugin = _mapping(
                current_plugins.setdefault("per_plugin", {}),
                "debug.plugins.per_plugin",
            )
            for name, value in submitted_per_plugin.items():
                if not isinstance(name, str) or not _PLUGIN_NAME_RE.fullmatch(name):
                    raise _validation_error(
                        "debug.plugins.per_plugin keys must be valid plugin names"
                    )
                if type(value) is not bool:
                    raise _validation_error(
                        f"debug.plugins.per_plugin.{name} must be a boolean"
                    )
                current_per_plugin[name] = value

    if "plugins_enabled" in submitted_debug:
        flat_enabled = submitted_debug["plugins_enabled"]
        if type(flat_enabled) is not bool:
            raise _validation_error("debug.plugins_enabled must be a boolean")
        current_plugins["enabled"] = flat_enabled
    elif "enabled" in current_plugins:
        flat_enabled = bool(current_plugins["enabled"])
    elif type(current_debug.get("plugins_enabled")) is bool:
        # Preserve legacy configs that predate debug.plugins.enabled.  A partial
        # update must migrate the flat value, not silently reset it to false.
        flat_enabled = current_debug["plugins_enabled"]
        current_plugins["enabled"] = flat_enabled
    else:
        flat_enabled = False
        current_plugins["enabled"] = False

    current_debug["plugins"] = current_plugins
    current_debug["plugins_enabled"] = flat_enabled
    return current_debug


def _apply_global_runtime(manager: Any) -> None:
    if bool(manager.get("hot_reload", False)):
        manager.start_watching()
    else:
        manager.stop_watching()

    if bool(manager.get("debug_mode", False)):
        log_level = "DEBUG"
    else:
        observability = manager.get("observability", {}) or {}
        log_level = (
            str(observability.get("log_level", "info")).upper()
            if isinstance(observability, dict)
            else "INFO"
        )
    from aigateway_core.shared.logger import setup_logging

    setup_logging(log_level=log_level)


class _GlobalRuntimeManager:
    """Delegate validation while including operational side effects in rollback."""

    def __init__(self, manager: Any) -> None:
        self._manager = manager

    def __getattr__(self, name: str) -> Any:
        return getattr(self._manager, name)

    def load(self) -> dict[str, Any]:
        loaded = self._manager.load()
        _apply_global_runtime(self._manager)
        return loaded


@router.put("/global-config")
async def update_global_config_transactional(
    request: Request,
    _auth: dict[str, Any] = Depends(authenticate_admin),
):
    manager = _manager(request)
    try:
        body = await _json_object(request)
        _reject_unknown(body, {"hot_reload", "debug_mode", "debug"})
        if not body:
            raise _validation_error("At least one configuration field is required")
        if "hot_reload" in body:
            _require_bool(body, "hot_reload")
        if "debug_mode" in body:
            _require_bool(body, "debug_mode")

        def mutate(candidate: dict[str, Any]) -> None:
            if "hot_reload" in body:
                candidate["hot_reload"] = body["hot_reload"]
            if "debug_mode" in body:
                candidate["debug_mode"] = body["debug_mode"]
            if "debug" in body:
                candidate["debug"] = _merge_debug_section(
                    candidate.get("debug", {}),
                    body["debug"],
                )

        commit = _transactional_mutation(
            request,
            manager,
            mutate,
            runtime_manager=_GlobalRuntimeManager(manager),
        )
    except Exception as exc:
        raise _config_error(exc) from exc
    return _versioned_response(
        {
            "hot_reload": bool(commit.get("hot_reload", False)),
            "debug_mode": bool(commit.get("debug_mode", False)),
            "debug": commit.get("debug"),
        },
        _commit_revision(commit, manager.config_path),
    )


def _route_conflicts(existing: Any, secured_routes: list[Any]) -> bool:
    existing_path = getattr(existing, "path", None)
    existing_methods = set(getattr(existing, "methods", set()) or set())
    if not existing_path or not existing_methods:
        return False
    for secured in secured_routes:
        if getattr(secured, "path", None) != existing_path:
            continue
        secured_methods = set(getattr(secured, "methods", set()) or set())
        if existing_methods & secured_methods:
            return True
    return False


def install_config_management_routes(admin_router: APIRouter) -> None:
    marker = "_aigateway_config_management_routes_installed"
    if getattr(admin_router, marker, False):
        return
    secured_routes = list(router.routes)
    admin_router.routes[:] = [
        existing
        for existing in admin_router.routes
        if not _route_conflicts(existing, secured_routes)
    ]
    admin_router.routes[0:0] = secured_routes
    setattr(admin_router, marker, True)


__all__ = ["install_config_management_routes", "router"]
