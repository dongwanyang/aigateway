"""
Routes — 基础设施路由
====================

实现以下接口（API_CONTRACT.md）：
- GET /metrics — Prometheus 指标端点
- GET /health — 健康检查端点

这些接口不需要鉴权（公开端点）。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response as FastAPIResponse
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

logger = logging.getLogger(__name__)

router = APIRouter()


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
        from aigateway_api.main import app
        metrics_collector = getattr(app.state, "metrics_collector")
        circuit_breaker_factory = getattr(app.state, "circuit_breaker_factory")

        # 更新熔断器状态指标
        if circuit_breaker_factory:
            for provider, breaker in circuit_breaker_factory._breakers.items():
                if metrics_collector:
                    metrics_collector.set_circuit_breaker_state(
                        provider=provider,
                        state=breaker.get_state_value(),
                    )

        raw = generate_latest()
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
    from aigateway_api.main import app
    s = app.state

    redis_mgr = getattr(s, "redis_manager")
    qdrant_mgr = getattr(s, "qdrant_manager")
    config_manager = getattr(s, "config_manager")
    circuit_breaker_factory = getattr(s, "circuit_breaker_factory")
    plugin_registry = getattr(s, "plugin_registry")
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
    plugins_status: Dict[str, Dict[str, Any]] = {}
    if plugin_registry:
        all_plugins = plugin_registry.get_all()
        for plugin in all_plugins:
            name = getattr(plugin, "name", "unknown")
            enabled = getattr(plugin, "enabled", True)
            plugins_status[name] = {
                "enabled": enabled,
                "status": "healthy" if enabled else "disabled",
            }

    # 构建熔断器状态
    cb_status: Dict[str, Dict[str, Any]] = {}
    if circuit_breaker_factory:
        for provider, breaker in circuit_breaker_factory._breakers.items():
            cb_status[provider] = breaker.get_status()

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

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

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
