"""
Routes — 基础设施路由
====================

实现以下接口（API_CONTRACT.md）：
- GET /metrics — Prometheus 指标端点
- GET /health — 健康检查端点

这些接口不需要鉴权（公开端点）。控制台专用聊天/视频端点虽然定义在本模块，
但使用 authenticate_admin 显式保护，并在调度前绑定服务端 API Key，
确保成本账本和配额仍按 API Key 维度执行。
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.responses import Response as FastAPIResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .auth_middleware import authenticate_admin, authenticate_api_key, require_scope
from .openai_compat import ChatCompletionRequest, _get_app_state

logger = logging.getLogger(__name__)

router = APIRouter()


def _console_chat_api_key() -> str:
    """Return the server-side API key used to account for control-panel chat."""
    return (
        os.environ.get("AI_GATEWAY_CONSOLE_CHAT_API_KEY", "").strip()
        or os.environ.get("ADMIN_API_KEY", "").strip()
    )


async def _bind_console_chat_api_key(
    request: Request,
    *,
    resource_owner: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind a real API-key principal before entering RequestDispatcher.

    /v1/* remains API-key-only. The browser session proves the operator is logged
    in to the control panel; this helper then attaches a server-side API key so
    quota checks, request accounting, and cost ledger updates still use the same
    key_hash path as machine clients.
    """
    key_value = _console_chat_api_key()
    if not key_value:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": {
                    "code": "console_chat_api_key_required",
                    "message": "Set AI_GATEWAY_CONSOLE_CHAT_API_KEY or ADMIN_API_KEY to enable control-panel chat.",
                }
            },
        )
    principal = await authenticate_api_key(request, api_key=key_value)
    require_scope(principal, "chat")
    if resource_owner is not None:
        # The server-side key remains the billing/quota principal, but media
        # drafts created from the console belong to the authenticated browser
        # operator. Never derive this owner from browser-supplied JSON.
        request.state.draft_owner = {
            "user_id": resource_owner.get("user_id") or None,
            "group_id": resource_owner.get("group_id") or None,
        }
    return principal


# ------------------------------------------------------------------
# POST /admin/console/chat/completions
# ------------------------------------------------------------------


@router.post("/admin/console/chat/completions")
async def post_console_chat_completions(
    body: ChatCompletionRequest,
    request: Request,
    _auth: dict[str, Any] = Depends(authenticate_admin),
) -> Any:
    """Control-panel chat endpoint authenticated by browser session.

    /v1/* remains API-key-only for machine clients. The control panel uses this
    admin-scoped endpoint so username/password browser sessions do not bypass the
    API-key boundary while still preserving the existing chat UX. Before dispatch
    it binds a server-side API key so quota/cost enforcement remains active.
    """
    from aigateway_api.dispatcher import RequestDispatcher

    await _bind_console_chat_api_key(request, resource_owner=_auth)

    state = _get_app_state(request)
    dispatcher = RequestDispatcher(state)
    return await dispatcher.dispatch(body, request)


@router.get("/admin/console/videos/{video_id}")
async def get_console_video_status(
    video_id: str,
    request: Request,
    _auth: dict[str, Any] = Depends(authenticate_admin),
) -> JSONResponse:
    """Poll video status from the control panel without exposing an API key."""
    await _bind_console_chat_api_key(request)

    state = _get_app_state(request)
    bridge = state.get("litellm_bridge")
    if bridge is None:
        return JSONResponse(
            content={"error": {"code": "bridge_unavailable", "message": "LiteLLM bridge not initialized"}},
            status_code=503,
        )
    try:
        result: dict[str, Any] = await bridge.retrieve_video(video_id)
        return JSONResponse(content=result)
    except Exception:
        logger.exception("Console video retrieval failed: %s", video_id)
        return JSONResponse(
            content={"error": {"code": "video_retrieve_failed", "message": "Video retrieval failed"}},
            status_code=502,
        )


# ------------------------------------------------------------------
# Admin config schema/table endpoints
# ------------------------------------------------------------------


def _config_path_from_state(request: Request) -> str:
    from .app_state import get_state

    state = get_state(request)
    config_manager = getattr(state, "config_manager", None)
    config_path = getattr(config_manager, "config_path", None)
    if not config_path:
        config_path = os.environ.get("AI_GATEWAY_CONFIG_PATH", "./config.yaml")
    if not os.path.isfile(config_path):
        raise HTTPException(
            status_code=500,
            detail={"error": {"code": "internal_error", "message": "Config file not found"}},
        )
    return config_path


def _read_yaml_config(path: str) -> dict[str, Any]:
    import yaml

    with open(path, "r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


def _write_yaml_config(path: str, data: dict[str, Any]) -> None:
    import errno
    import fcntl

    import yaml

    lock_path = path + ".lock"
    with open(lock_path, "w", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HTTPException(
                status_code=409,
                detail={"error": {"code": "config_update_busy", "message": "Another configuration update is in progress"}},
            ) from exc

        config_dir = os.path.dirname(os.path.abspath(path)) or "."
        if os.path.ismount(path):
            with open(path, "w", encoding="utf-8") as file:
                yaml.dump(data, file, default_flow_style=False, allow_unicode=True, sort_keys=False)
                file.flush()
            fcntl.flock(lock_file, fcntl.LOCK_UN)
            return

        fd, tmp_path = tempfile.mkstemp(prefix=".config.yaml.", suffix=".tmp", dir=config_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                yaml.dump(data, file, default_flow_style=False, allow_unicode=True, sort_keys=False)
            try:
                os.replace(tmp_path, path)
            except OSError as exc:
                if exc.errno not in (errno.EBUSY, errno.EXDEV, errno.ENOTSUP, errno.EPERM):
                    raise
                with open(path, "w", encoding="utf-8") as file:
                    yaml.dump(data, file, default_flow_style=False, allow_unicode=True, sort_keys=False)
                    file.flush()
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _template_candidates(config_path: str) -> list[str]:
    here = os.path.abspath(os.path.dirname(__file__))
    return [
        os.environ.get("AI_GATEWAY_CONFIG_TEMPLATE_PATH", ""),
        os.path.join(os.getcwd(), "config.yaml.template"),
        os.path.join(os.path.dirname(os.path.abspath(config_path)), "config.yaml.template"),
        os.path.abspath(os.path.join(here, "..", "..", "..", "config.yaml.template")),
        os.path.abspath(os.path.join(here, "..", "..", "..", "..", "config.yaml.template")),
    ]


def _clean_inline_comment(comment: str) -> str:
    comment = comment.strip()
    comment = re.sub(r"^=+\s*", "", comment)
    comment = re.sub(r"\s*=+$", "", comment)
    return comment.strip()


def _parse_template_schema(config_path: str) -> list[dict[str, Any]]:
    """Extract table descriptions from config.yaml.template comments.

    The template remains the source of truth for parameter descriptions. The
    frontend receives only generated metadata, so descriptions do not drift into
    React code.
    """
    template_path = next((p for p in _template_candidates(config_path) if p and os.path.isfile(p)), "")
    if not template_path:
        return []

    stack: list[tuple[int, str]] = []
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    key_re = re.compile(r"^(?P<indent>\s*)(?P<list>-\s+)?(?P<key>[A-Za-z0-9_\-]+)\s*:\s*(?P<rest>.*)$")
    with open(template_path, "r", encoding="utf-8") as file:
        for raw_line in file:
            if not raw_line.strip() or raw_line.lstrip().startswith("#"):
                continue
            match = key_re.match(raw_line)
            if not match:
                continue
            indent = len(match.group("indent"))
            is_list_item = bool(match.group("list"))
            key = match.group("key")
            rest = match.group("rest")

            while stack and stack[-1][0] >= indent:
                stack.pop()
            parent_parts = [part for _, part in stack]
            if is_list_item:
                if parent_parts:
                    parent_parts[-1] = parent_parts[-1] + "[]"
                else:
                    parent_parts.append("[]")
            path = ".".join([*parent_parts, key]).strip(".")
            if not path:
                continue

            comment = ""
            if "#" in rest:
                comment = _clean_inline_comment(rest.split("#", 1)[1])
            if comment and path not in seen:
                items.append({
                    "path": path,
                    "module": path.split(".", 1)[0].replace("[]", ""),
                    "description": comment,
                })
                seen.add(path)

            # Treat mapping keys with no scalar value as a parent path.
            value_part = rest.split("#", 1)[0].strip()
            if value_part == "":
                if is_list_item and parent_parts:
                    stack.append((indent, parent_parts[-1]))
                else:
                    stack.append((indent, key))

    return items


def _preserve_masked_provider_keys(new_config: dict[str, Any], current_config: dict[str, Any]) -> None:
    providers = new_config.get("providers")
    current_providers = current_config.get("providers", {})
    if not isinstance(providers, dict) or not isinstance(current_providers, dict):
        return
    for provider_name, provider_cfg in providers.items():
        if not isinstance(provider_cfg, dict):
            continue
        key_value = provider_cfg.get("api_key")
        if isinstance(key_value, str) and key_value.endswith("***"):
            original = current_providers.get(provider_name, {})
            if isinstance(original, dict):
                provider_cfg["api_key"] = original.get("api_key", key_value)


@router.get("/admin/config/schema")
async def get_config_schema(
    request: Request,
    _auth: dict[str, Any] = Depends(authenticate_admin),
):
    """Return parameter descriptions for the control-panel table editor."""
    config_path = _config_path_from_state(request)
    return {
        "data": {"items": _parse_template_schema(config_path)},
        "message": "success",
    }


@router.put("/admin/config/table")
async def update_table_config(
    request: Request,
    _auth: dict[str, Any] = Depends(authenticate_admin),
):
    """Write the full table-edited config.yaml.

    This endpoint is intentionally separate from PUT /admin/config, whose legacy
    whitelist predates the table editor. It accepts every top-level section that
    already exists in config.yaml, preserving masked provider API keys.
    """
    from .app_state import get_state

    try:
        new_config = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "validation_error", "message": "Invalid JSON body"}},
        ) from exc
    if not isinstance(new_config, dict):
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "validation_error", "message": "Config body must be an object"}},
        )

    config_path = _config_path_from_state(request)
    current_config = _read_yaml_config(config_path)
    _preserve_masked_provider_keys(new_config, current_config)
    _write_yaml_config(config_path, new_config)

    state = get_state(request)
    config_manager = getattr(state, "config_manager", None)
    if config_manager is not None:
        config_manager.load()

    return {"data": {"updated": True}, "message": "success"}


# ------------------------------------------------------------------
# GET /metrics
# ------------------------------------------------------------------


@router.get("/metrics")
async def get_metrics(request: Request) -> FastAPIResponse:
    """Prometheus 指标端点。

    返回 Prometheus 格式的指标文本（text/plain）。

    API_CONTRACT.md: GET /metrics 成功响应
    Content-Type: text/plain; version=0.0.4; charset=utf-8
    """
    from starlette.responses import Response as StarletteResponse

    try:
        from .app_state import get_state
        state_obj = get_state(request)
        metrics_collector = state_obj.metrics_collector
        litellm_bridge = getattr(state_obj, "litellm_bridge", None)

        # 更新熔断器状态指标(从 litellm cooldown tracker 读,按 provider 聚合)
        if litellm_bridge and metrics_collector and hasattr(litellm_bridge, "get_cooldown_status_by_provider"):
            provider_states = litellm_bridge.get_cooldown_status_by_provider()
            for provider, state in provider_states.items():
                metrics_collector.set_circuit_breaker_state(
                    provider=provider,
                    state=state,
                )

        # 单 worker 模式：使用 MetricsCollector 持有的 registry
        if metrics_collector and metrics_collector._registry is not None:
            raw = generate_latest(metrics_collector._registry)
        else:
            from prometheus_client import CollectorRegistry
            raw = generate_latest(CollectorRegistry())
        return StarletteResponse(
            content=raw,
            status_code=200,
            media_type=CONTENT_TYPE_LATEST,
        )
    except Exception as exc:
        logger.error("Failed to collect metrics: %s", exc)
        return StarletteResponse(
            content=json.dumps({"error": {"code": "internal_error", "message": "Failed to collect metrics"}}),
            status_code=500,
            media_type="application/json",
        )


# ------------------------------------------------------------------
# GET /health
# ------------------------------------------------------------------


@router.get("/health")
async def get_health(request: Request) -> JSONResponse:
    """健康检查端点。

    API_CONTRACT.md: GET /health 成功响应
    返回各依赖服务的健康状态。
    """
    from .app_state import get_state
    s = get_state(request)

    redis_mgr = s.redis_manager
    qdrant_mgr = s.qdrant_manager
    plugin_registry = s.plugin_registry
    start_time = getattr(s, "_start_time", 0)

    # 检查 Redis
    redis_status = "disconnected"
    redis_latency = 0.0
    if redis_mgr and redis_mgr.redis:
        try:
            start = time.time()
            await redis_mgr.redis.ping()
            redis_latency = round((time.time() - start) * 1000, 2)
            redis_status = "connected"
        except Exception as exc:
            redis_status = "error"
            logger.warning("Redis health check failed: %s", exc)

    # 检查 Qdrant
    qdrant_status = "disconnected"
    qdrant_latency = 0.0
    if qdrant_mgr and qdrant_mgr._http:
        try:
            start = time.time()
            resp = await qdrant_mgr._http.get("/")
            resp.raise_for_status()
            qdrant_latency = round((time.time() - start) * 1000, 2)
            qdrant_status = "connected"
        except Exception as exc:
            qdrant_status = "error"
            logger.warning("Qdrant health check failed: %s", exc)

    # 构建插件状态
    plugins_status: dict[str, dict[str, Any]] = {}
    if plugin_registry:
        all_plugins = plugin_registry.get_all()
        for plugin in all_plugins:
            name = getattr(plugin, "name", "unknown")
            enabled = getattr(plugin, "enabled", True)
            plugins_status[name] = {
                "enabled": enabled,
                "status": "healthy" if enabled else "disabled",
            }

    # 构建熔断器状态(从 litellm bridge tracker 读)
    litellm_bridge_for_cb = getattr(s, "litellm_bridge", None)
    if litellm_bridge_for_cb is not None and hasattr(litellm_bridge_for_cb, "get_cooldown_status"):
        litellm_bridge_for_cb.get_cooldown_status()

    # 确定整体状态
    dependencies = {
        "redis": {"status": redis_status, "latency_ms": redis_latency},
        "qdrant": {"status": qdrant_status, "latency_ms": qdrant_latency},
    }

    critical_deps_down = all(
        deps.get("status") in ("disconnected", "error")
        for deps in dependencies.values()
    )

    if critical_deps_down:
        overall_status = "unhealthy"
    elif any(deps.get("status") == "error" for deps in dependencies.values()):
        overall_status = "degraded"
    else:
        overall_status = "healthy"

    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    return JSONResponse(content={
        "data": {
            "status": overall_status,
            "version": "1.0.0",
            "uptime_seconds": int(time.time() - start_time) if start_time else 0,
            "timestamp": now_iso,
            "dependencies": dependencies,
            "plugins": plugins_status,
        },
        "message": "success" if overall_status == "healthy" else "partial degradation",
    })
