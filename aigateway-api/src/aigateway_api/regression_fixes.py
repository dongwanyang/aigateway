"""Targeted runtime fixes installed before the API application is created."""

from __future__ import annotations

import asyncio
import copy
import fcntl
import os
import re
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool


def _remove_route(router: Any, path: str, method: str) -> None:
    router.routes[:] = [
        route
        for route in router.routes
        if not (
            getattr(route, "path", None) == path
            and method in set(getattr(route, "methods", set()) or set())
        )
    ]


def safe_flocked_inplace_write(config_path: str, file_config: dict[str, Any]) -> None:
    """Lock the existing inode before truncating a bind-mounted YAML file."""
    import yaml

    flags = os.O_RDWR | os.O_CREAT
    fd = os.open(config_path, flags, 0o600)
    try:
        with os.fdopen(fd, "r+", encoding="utf-8", closefd=False) as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                stream.seek(0)
                stream.truncate(0)
                yaml.dump(
                    file_config,
                    stream,
                    default_flow_style=False,
                    allow_unicode=True,
                    sort_keys=False,
                )
                stream.flush()
                os.fsync(stream.fileno())
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _parse_prometheus_labels(raw: str) -> dict[str, str]:
    """Parse Prometheus labels without splitting commas inside quoted values."""
    labels: dict[str, str] = {}
    pattern = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"\\])*)"(?:,|$)')
    position = 0
    while position < len(raw):
        match = pattern.match(raw, position)
        if match is None:
            break
        key, encoded = match.groups()
        labels[key] = bytes(encoded, "utf-8").decode("unicode_escape")
        position = match.end()
    return labels


class PluginToggleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1)
    enabled: StrictBool


async def _update_quota_fixed(
    request: Request,
    key_id: str,
    body: Any,
) -> dict[str, Any]:
    from .admin_routes import _get_auth_defaults, _get_keystore_and_metrics

    key_store, _ = _get_keystore_and_metrics(request)
    if key_store is None:
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": "Auth store not initialized"}})
    if not key_id.startswith("key_"):
        raise HTTPException(status_code=400, detail={"error": {"code": "validation_error", "message": "Invalid key_id format"}})

    key_hashes = await key_store._find_key_hashes_by_id(key_id)
    if not key_hashes:
        raise HTTPException(status_code=404, detail={"error": {"code": "not_found", "message": f"API key '{key_id}' not found"}})
    key_hash = key_hashes[0]
    current = await key_store.get_api_key(key_hash)
    if not current:
        raise HTTPException(status_code=404, detail={"error": {"code": "not_found", "message": f"API key '{key_id}' not found"}})

    updated_fields: dict[str, str] = {}
    mapping = {
        "daily_tokens": "daily_tokens_limit",
        "monthly_cost": "monthly_cost_limit",
        "rate_limit_rpm": "rate_limit_rpm",
        "rate_limit_tpm": "rate_limit_tpm",
    }
    for source, target in mapping.items():
        value = getattr(body, source, None)
        if value is not None:
            updated_fields[target] = str(value)
    if not updated_fields:
        raise HTTPException(status_code=400, detail={"error": {"code": "validation_error", "message": "No fields to update"}})

    await key_store.set_api_key(key_hash, updated_fields)
    refreshed = await key_store.get_api_key(key_hash) or {**current, **updated_fields}
    defaults = _get_auth_defaults()
    return {
        "data": {
            "id": key_id,
            "user_id": refreshed.get("user_id", ""),
            "quotas": {
                "daily_tokens_limit": int(refreshed.get("daily_tokens_limit", defaults["daily_tokens"])),
                "monthly_cost_limit": float(refreshed.get("monthly_cost_limit", defaults["monthly_cost"])),
                "rate_limit_rpm": int(refreshed.get("rate_limit_rpm", defaults["rate_limit_rpm"])),
                "rate_limit_tpm": int(refreshed.get("rate_limit_tpm", defaults["rate_limit_tpm"])),
            },
        },
        "message": "success",
    }


async def probe_comfyui_resilient(comfy: dict[str, Any]) -> dict[str, Any]:
    """Return partial ComfyUI status when one read-only endpoint is unavailable."""
    from .local_generation import _config_number, _config_text, builtin_presets

    server_url = _config_text(comfy, "server_url").rstrip("/")
    public_url = _config_text(comfy, "public_url").rstrip("/")
    manager_url = _config_text(comfy, "manager_url").rstrip("/") or public_url
    models_path = _config_text(comfy, "models_path")
    config_errors = [
        f"config_missing:{key}"
        for key, value in (("server_url", server_url), ("public_url", public_url), ("models_path", models_path))
        if not value
    ]
    config_errors.extend(sorted({
        error
        for preset in builtin_presets(comfy)
        for error in preset.get("configuration_errors", [])
        if isinstance(error, str) and error
    }))
    result: dict[str, Any] = {
        "available": False,
        "manager_enabled": bool(comfy.get("manager_enabled", False)),
        "public_url": public_url,
        "manager_url": manager_url,
        "gpu": None,
        "queue": None,
        "available_nodes": [],
        "disk": None,
        "configuration_status": "configuration_error" if config_errors else "configured",
        "configuration_errors": config_errors,
        "endpoint_errors": {},
        "error": config_errors[0] if config_errors else None,
    }
    if not server_url:
        return result

    try:
        timeout = httpx.Timeout(
            _config_number(comfy, "connect_timeout"),
            read=_config_number(comfy, "read_timeout", fallback_key="execution_timeout"),
        )
    except (TypeError, ValueError) as exc:
        result["error"] = str(exc)
        return result

    endpoints = {
        "system_stats": f"{server_url}/system_stats",
        "object_info": f"{server_url}/object_info",
        "queue": f"{server_url}/queue",
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        responses = await asyncio.gather(
            *(client.get(url) for url in endpoints.values()),
            return_exceptions=True,
        )
    payloads: dict[str, Any] = {}
    for name, response in zip(endpoints, responses, strict=True):
        if isinstance(response, Exception):
            result["endpoint_errors"][name] = type(response).__name__
            continue
        try:
            response.raise_for_status()
            payloads[name] = response.json()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            result["endpoint_errors"][name] = type(exc).__name__

    stats = payloads.get("system_stats")
    if isinstance(stats, dict):
        devices = stats.get("devices", [])
        result["gpu"] = devices[0] if isinstance(devices, list) and devices else None
    objects = payloads.get("object_info")
    if isinstance(objects, dict):
        result["available_nodes"] = sorted(objects)
    queue = payloads.get("queue")
    if isinstance(queue, dict):
        result["queue"] = {
            "running": len(queue.get("queue_running", [])),
            "pending": len(queue.get("queue_pending", [])),
        }
    result["available"] = bool(payloads)
    if result["endpoint_errors"] and not result["error"]:
        result["error"] = "partial_probe_failure"

    if models_path:
        try:
            import shutil
            usage = await asyncio.to_thread(shutil.disk_usage, models_path)
            result["disk"] = {"total_bytes": usage.total, "free_bytes": usage.free}
        except OSError:
            pass
    return result


def _install_cpu_safe_l3_default() -> None:
    """Keep shared single-GPU deployments on CPU unless CUDA is explicit."""
    from aigateway_core.prefix.cache import l3_semantic

    original = l3_semantic.set_l3_device

    def set_l3_device_safe(device: str) -> None:
        normalized = (device or "auto").strip().lower()
        allow_shared_cuda = os.getenv("AI_GATEWAY_L3_ALLOW_CUDA", "").strip().lower() in {"1", "true", "yes"}
        if normalized == "auto" and not allow_shared_cuda:
            normalized = "cpu"
        original(normalized)

    l3_semantic.set_l3_device = set_l3_device_safe


def install_runtime_regression_fixes() -> None:
    from . import admin_routes, local_generation
    from .auth_middleware import authenticate_admin

    admin_routes._flocked_inplace_write = safe_flocked_inplace_write
    local_generation.probe_comfyui = probe_comfyui_resilient
    _install_cpu_safe_l3_default()

    _remove_route(admin_routes.router, "/api-keys/{key_id}", "PUT")
    _remove_route(admin_routes.router, "/plugins-config", "PUT")
    _remove_route(admin_routes.router, "/metrics-json", "GET")

    @admin_routes.router.put("/api-keys/{key_id}")
    async def update_api_key_quota_fixed(
        request: Request,
        key_id: str,
        body: admin_routes.UpdateQuotaRequest,
        _auth: dict[str, Any] = Depends(authenticate_admin),
    ) -> dict[str, Any]:
        return await _update_quota_fixed(request, key_id, body)

    @admin_routes.router.put("/plugins-config")
    async def update_plugins_config_fixed(
        body: PluginToggleRequest,
        request: Request,
        _auth: dict[str, Any] = Depends(authenticate_admin),
    ) -> dict[str, Any]:
        from .app_state import get_state

        state = get_state(request)
        manager = state.config_manager
        if manager is None or not manager.config_path or not Path(manager.config_path).is_file():
            raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": "Config file not found"}})
        generation_path = admin_routes._GENERATION_PLUGIN_CONFIG_PATH.get(body.name)

        def update(file_config: dict[str, Any]) -> None:
            plugins = file_config.get("plugins", [])
            changed = False
            for plugin in plugins:
                if isinstance(plugin, dict) and plugin.get("name") == body.name:
                    plugin["enabled"] = body.enabled
                    changed = True
                    break
            if not changed and generation_path:
                current = file_config.setdefault("generation_optimization", {})
                current["enabled"] = True
                for key in generation_path[:-1]:
                    current = current.setdefault(key, {})
                current[generation_path[-1]] = body.enabled
                changed = True
            if not changed:
                raise HTTPException(status_code=404, detail={"error": {"code": "not_found", "message": f"Plugin '{body.name}' not found"}})

        persisted = admin_routes._locked_update_yaml(manager.config_path, update)
        next_config = copy.deepcopy(manager._config)
        if generation_path:
            manager._set_nested(next_config, "generation_optimization.enabled", True)
            manager._set_nested(next_config, "generation_optimization." + ".".join(generation_path), body.enabled)
        else:
            manager._set_nested(next_config, "plugins", persisted.get("plugins", []))
        manager.atomic_swap(next_config)
        return {"data": {"name": body.name, "enabled": body.enabled}, "message": "success"}

    @admin_routes.router.get("/metrics-json")
    async def get_metrics_json_fixed(
        request: Request,
        _auth: dict[str, Any] = Depends(authenticate_admin),
    ) -> dict[str, Any]:
        from prometheus_client import generate_latest
        from .app_state import get_state

        state = get_state(request)
        collector = state.metrics_collector
        raw = generate_latest(collector._registry).decode("utf-8") if collector and collector._registry is not None else ""
        samples: dict[str, list[dict[str, Any]]] = {}
        metric_pattern = re.compile(r"^(\w+)(?:\{(.*)\})?\s+([^\s]+)$")
        for line in raw.splitlines():
            if not line or line.startswith("#"):
                continue
            match = metric_pattern.match(line)
            if match is None:
                continue
            name, labels_raw, value_raw = match.groups()
            try:
                value = float(value_raw)
            except ValueError:
                continue
            samples.setdefault(name, []).append({
                "labels": _parse_prometheus_labels(labels_raw or ""),
                "value": value,
            })

        key_stats = {"total_keys": 0, "total_daily_tokens_used": 0, "total_monthly_cost_used": 0.0}
        if state.key_store:
            rows = state.key_store.conn.fetchall("SELECT daily_tokens_used, monthly_cost_used FROM api_keys WHERE status='active'")
            for row in rows:
                key_stats["total_keys"] += 1
                key_stats["total_daily_tokens_used"] += int(row["daily_tokens_used"])
                key_stats["total_monthly_cost_used"] += float(row["monthly_cost_used"])
        bridge = getattr(state, "litellm_bridge", None)
        breakers = bridge.get_cooldown_status() if bridge is not None and hasattr(bridge, "get_cooldown_status") else {}
        return {
            "data": {
                "prometheus": samples,
                "keys": key_stats,
                "circuit_breakers": breakers,
                "uptime_seconds": collector.get_uptime_seconds() if collector else 0,
            },
            "message": "success",
        }
