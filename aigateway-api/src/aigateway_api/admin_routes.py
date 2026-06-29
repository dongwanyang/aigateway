"""
Admin Routes — 管理接口实现
==========================

实现以下接口（API_CONTRACT.md）：
- GET  /admin/api-keys       — 列出 API Key
- POST /admin/api-keys       — 创建 API Key
- DELETE /admin/api-keys/{key_id} — 撤销 API Key
- GET  /admin/quotas/{key_id}    — 查询配额详情

所有接口需要管理员权限鉴权（由 auth_middleware 处理）。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

from .auth_middleware import authenticate_admin

router = APIRouter()


# ------------------------------------------------------------------
# 请求模型
# ------------------------------------------------------------------


class CreateApiKeyRequest(BaseModel):
    """POST /admin/api-keys 请求体。"""

    user_id: str = Field(..., min_length=1, description="关联的用户 ID")
    daily_tokens: Optional[int] = Field(default=1_000_000, description="每日 token 上限")
    monthly_cost: Optional[float] = Field(default=50.00, description="每月成本上限（美元）")
    rate_limit_rpm: Optional[int] = Field(default=60, description="每分钟请求数上限")
    rate_limit_tpm: Optional[int] = Field(default=100_000, description="每分钟 token 数上限")


# ------------------------------------------------------------------
# 辅助函数
# ------------------------------------------------------------------


def _get_keystore_and_metrics(request: Request) -> tuple[Any, Any]:
    """从 app.state 获取 KeyStore 和 MetricsCollector。"""
    from aigateway_api.main import app
    return getattr(app.state, "key_store"), getattr(app.state, "metrics_collector")


def _format_quota_item(key_data: Dict[str, Any], key_hash: str) -> Dict[str, Any]:
    """格式化单个 API Key 的配额信息。"""
    daily_limit = int(key_data.get("daily_tokens_limit", 1_000_000))
    daily_used = int(key_data.get("daily_tokens_used", 0))
    monthly_limit = float(key_data.get("monthly_cost_limit", 50.00))
    monthly_used = float(key_data.get("monthly_cost_used", 0.00))
    rpm_limit = int(key_data.get("rate_limit_rpm", 60))
    tpm_limit = int(key_data.get("rate_limit_tpm", 100_000))

    # 获取当前 RPM/TPM 窗口计数
    rpm_current = int(key_data.get("rpm_window_count", 0))
    tpm_current = int(key_data.get("tpm_window_count", 0))

    return {
        "id": key_data.get("key_id", ""),
        "key_prefix": key_data.get("key_prefix", ""),
        "user_id": key_data.get("user_id", ""),
        "created_at": key_data.get("created_at", ""),
        "last_used_at": key_data.get("last_used_at") or None,
        "status": key_data.get("status", "active"),
        "quotas": {
            "daily_tokens_used": daily_used,
            "daily_tokens_limit": daily_limit,
            "monthly_cost_used": round(monthly_used, 2),
            "monthly_cost_limit": monthly_limit,
            "rpm_current": rpm_current,
            "rpm_limit": rpm_limit,
            "tpm_current": tpm_current,
            "tpm_limit": tpm_limit,
        },
        "usage_percentage": {
            "daily_tokens": round(daily_used / daily_limit, 4) if daily_limit > 0 else 0.0,
            "monthly_cost": round(monthly_used / monthly_limit, 4) if monthly_limit > 0 else 0.0,
        },
    }


# ------------------------------------------------------------------
# GET /admin/api-keys
# ------------------------------------------------------------------


@router.get("/api-keys")
async def list_api_keys(
    request: Request,
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页数量"),
    _auth: Dict[str, Any] = Depends(authenticate_admin),
):
    """列出所有 API Key 及其配额使用情况。"""
    key_store, metrics = _get_keystore_and_metrics(request)
    redis_mgr = key_store.redis

    if redis_mgr is None or redis_mgr.redis is None:
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": "Redis not connected"}})

    # 扫描所有 API Key
    cursor = 0
    all_keys: List[Dict[str, Any]] = []
    while True:
        cursor, keys = await redis_mgr.redis.scan(
            cursor, match="aigateway:key:*", count=100
        )
        for raw_key in keys:
            key_str = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
            kh = key_str.split(":")[-1]
            data = await redis_mgr.get_api_key(kh)
            if data:
                data["_key_hash"] = kh  # 保留 key_hash 以便后续操作
                all_keys.append(data)
        if cursor == 0:
            break

    # 分页
    total = len(all_keys)
    start = (page - 1) * page_size
    end = start + page_size
    paginated = all_keys[start:end]

    items = [_format_quota_item(k, k.get("_key_hash", "")) for k in paginated]

    return {
        "data": {
            "items": items,
            "pagination": {
                "page": page,
                "pageSize": page_size,
                "total": total,
            },
        },
        "message": "success",
    }


# ------------------------------------------------------------------
# POST /admin/api-keys
# ------------------------------------------------------------------


@router.post("/api-keys")
async def create_api_key(
    request: Request,
    body: CreateApiKeyRequest,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
):

    """创建新的 API Key。"""
    key_store, _ = _get_keystore_and_metrics(request)

    # 验证配额参数
    if body.daily_tokens is not None and body.daily_tokens <= 0:
        raise HTTPException(status_code=400, detail={"error": {"code": "validation_error", "message": "daily_tokens must be a positive integer"}})
    if body.monthly_cost is not None and body.monthly_cost <= 0:
        raise HTTPException(status_code=400, detail={"error": {"code": "validation_error", "message": "monthly_cost must be a positive number"}})

    quotas = {
        "daily_tokens": body.daily_tokens,
        "monthly_cost": body.monthly_cost,
        "rate_limit_rpm": body.rate_limit_rpm,
        "rate_limit_tpm": body.rate_limit_tpm,
    }

    try:
        result = await key_store.create(user_id=body.user_id, quotas=quotas)
    except ValueError as exc:
        # 检查是否是重复 user_id
        if "已存在活跃" in str(exc) or "already" in str(exc).lower():
            raise HTTPException(status_code=409, detail={"error": {"code": "conflict", "message": f"User '{body.user_id}' already has an active key"}})
        raise HTTPException(status_code=400, detail={"error": {"code": "validation_error", "message": str(exc)}})
    except Exception as exc:
        logger.error("Failed to create API key: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": "Failed to create API key"}})

    return {"data": result, "message": "success"}


# ------------------------------------------------------------------
# DELETE /admin/api-keys/{key_id}
# ------------------------------------------------------------------


@router.delete("/api-keys/{key_id}")
async def delete_api_key(
    request: Request,
    key_id: str,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
):

    """撤销指定的 API Key。"""
    key_store, _ = _get_keystore_and_metrics(request)

    if not key_id.startswith("key_"):
        raise HTTPException(status_code=400, detail={"error": {"code": "validation_error", "message": "Invalid key_id format"}})

    success = await key_store.revoke(key_id)
    if not success:
        raise HTTPException(status_code=404, detail={"error": {"code": "not_found", "message": f"API key '{key_id}' not found"}})

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "data": {
            "id": key_id,
            "status": "revoked",
            "revoked_at": now_iso,
        },
        "message": "success",
    }


# ------------------------------------------------------------------
# GET /admin/metrics-json — Prometheus 指标（JSON 格式）
# ------------------------------------------------------------------


@router.get("/metrics-json")
async def get_metrics_json(
    request: Request,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
):

    """返回 Prometheus 指标的 JSON 格式，供前端仪表板使用。"""
    from aigateway_api.main import app
    s = app.state
    metrics_collector = getattr(s, "metrics_collector")
    circuit_breaker_factory = getattr(s, "circuit_breaker_factory")
    key_store = getattr(s, "key_store")

    # 收集 Prometheus 指标
    prom_samples: Dict[str, Any] = {}
    try:
        from prometheus_client import generate_latest
        # 单 worker 模式：使用 MetricsCollector 持有的 registry
        if metrics_collector and metrics_collector._registry is not None:
            raw = generate_latest(metrics_collector._registry).decode("utf-8")
        else:
            raw = ""
        for line in raw.split("\n"):
            if not line or line.startswith("#"):
                continue
            m = re.match(r"^(\w+)\{?([^}]*)\}?\s+(.+)$", line)
            if m:
                name, labels_str, value = m.groups()
                labels = {}
                if labels_str:
                    for pair in labels_str.split(","):
                        kv = pair.split("=")
                        if len(kv) == 2:
                            labels[kv[0]] = kv[1].strip('"')
                prom_samples[name] = {"labels": labels, "value": float(value)}
    except Exception as exc:
        logger.warning("Failed to collect Prometheus metrics: %s", exc)

    # 收集 KeyStore 聚合数据
    key_stats: Dict[str, Any] = {"total_keys": 0, "total_daily_tokens_used": 0, "total_monthly_cost_used": 0.0, "total_requests": 0}
    if key_store and key_store.redis and key_store.redis.redis:
        cursor = 0
        while True:
            cursor, keys = await key_store.redis.redis.scan(cursor, match="aigateway:key:*", count=100)
            for raw_key in keys:
                kh = raw_key.decode().split(":")[-1] if isinstance(raw_key, bytes) else raw_key.split(":")[-1]
                data = await key_store.redis.get_api_key(kh)
                if data:
                    key_stats["total_keys"] += 1
                    key_stats["total_daily_tokens_used"] += int(data.get("daily_tokens_used", 0))
                    key_stats["total_monthly_cost_used"] += float(data.get("monthly_cost_used", 0))
                    key_stats["total_requests"] += int(data.get("daily_tokens_used", 0))
            if cursor == 0:
                break

    # 熔断器状态
    cb_states: Dict[str, Any] = {}
    if circuit_breaker_factory:
        for provider, breaker in circuit_breaker_factory._breakers.items():
            cb_states[provider] = breaker.get_status()

    return {
        "data": {
            "prometheus": prom_samples,
            "keys": key_stats,
            "circuit_breakers": cb_states,
            "uptime_seconds": metrics_collector.get_uptime_seconds() if metrics_collector else 0,
        },
        "message": "success",
    }


# ------------------------------------------------------------------
# GET /admin/plugins-config — 插件配置（真实数据）
# ------------------------------------------------------------------


@router.get("/plugins-config")
async def get_plugins_config(
    request: Request,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
):

    """返回当前 config.yaml 中实际的插件配置。

    多 worker 场景下，内存中的 _config 可能过期，
    直接从文件读取确保返回最新值。
    """
    from aigateway_api.main import app
    s = app.state
    config_manager = getattr(s, "config_manager")

    # 直接从 YAML 文件读取最新配置（绕过可能有 stale 数据的内存缓存）
    plugins = []
    if config_manager:
        try:
            import os
            import yaml
            config_path = config_manager.config_path
            if config_path and os.path.isfile(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    raw = yaml.safe_load(f) or {}
                raw_plugins = raw.get("plugins", [])
                for p in raw_plugins:
                    if isinstance(p, dict):
                        plugins.append({
                            "name": p.get("name", "unknown"),
                            "enabled": p.get("enabled", True),
                            "depends_on": p.get("depends_on", []),
                            "config": p.get("config", {}),
                        })
        except Exception as exc:
            logger.warning("读取插件配置失败，回退到内存缓存: %s", exc)
            # 回退：从内存缓存读取
            plugins_cfg = config_manager.get("plugins", [])
            for p in plugins_cfg:
                if isinstance(p, dict):
                    plugins.append({
                        "name": p.get("name", "unknown"),
                        "enabled": p.get("enabled", True),
                        "depends_on": p.get("depends_on", []),
                        "config": p.get("config", {}),
                    })

    return {
        "data": {"plugins": plugins},
        "message": "success",
    }


@router.put("/plugins-config")
async def update_plugins_config(
    request: Request,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
):

    """更新插件配置（启用/禁用）。

    多 worker 场景下，内存缓存可能过期，直接从文件读取最新配置。
    使用文件锁防止并发写冲突。
    """
    import fcntl
    import os
    import yaml
    from pydantic import BaseModel, Field

    class PluginToggleRequest(BaseModel):
        name: str = Field(..., min_length=1)
        enabled: bool

    body: PluginToggleRequest
    try:
        # 解析请求体
        raw = await request.json()
        name = raw.get("name", "")
        enabled = raw.get("enabled", True)
    except Exception:
        raise HTTPException(status_code=400, detail={"error": {"code": "validation_error", "message": "Invalid request body"}})

    if not name:
        raise HTTPException(status_code=400, detail={"error": {"code": "validation_error", "message": "Plugin name is required"}})

    from aigateway_api.main import app
    s = app.state
    config_manager = getattr(s, "config_manager")

    if not config_manager:
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": "ConfigManager not initialized"}})

    # 从文件读取最新插件配置（绕过可能过期的内存缓存）
    config_path = config_manager.config_path
    if not config_path or not os.path.isfile(config_path):
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": "Config file not found"}})

    # 文件锁：确保同一时刻只有一个 worker 读写 config.yaml
    lock_path = config_path + ".lock"
    lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        with open(config_path, "r", encoding="utf-8") as f:
            file_config = yaml.safe_load(f) or {}

        plugins_cfg = file_config.get("plugins", [])
        updated = False
        for p in plugins_cfg:
            if isinstance(p, dict) and p.get("name") == name:
                p["enabled"] = enabled
                updated = True
                break

        if not updated:
            raise HTTPException(status_code=404, detail={"error": {"code": "not_found", "message": f"Plugin '{name}' not found"}})

        # 写回文件（只写 plugins 节）
        file_config["plugins"] = plugins_cfg
        writable_keys = {"server", "auth", "plugins", "providers", "embedding", "observability"}
        clean_config = {k: v for k, v in file_config.items() if k in writable_keys}
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(clean_config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

        # 同步更新内存缓存
        config_manager._set_nested(config_manager._config, "plugins", plugins_cfg)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()

    return {
        "data": {"name": name, "enabled": enabled},
        "message": "success",
    }


@router.get("/quotas/{key_id}")
async def get_quota(
    request: Request,
    key_id: str,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
):

    """查询指定 API Key 的详细配额状态。"""
    key_store, _ = _get_keystore_and_metrics(request)
    redis_mgr = key_store.redis

    if redis_mgr is None or redis_mgr.redis is None:
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": "Redis not connected"}})

    # 通过 key_id 查找 key_hash
    key_hashes = await key_store._find_key_hashes_by_id(key_id)
    if not key_hashes:
        raise HTTPException(status_code=404, detail={"error": {"code": "not_found", "message": f"API key '{key_id}' not found"}})

    kh = key_hashes[0]
    data = await redis_mgr.get_api_key(kh)
    if not data:
        raise HTTPException(status_code=404, detail={"error": {"code": "not_found", "message": f"API key '{key_id}' not found"}})

    daily_limit = int(data.get("daily_tokens_limit", 1_000_000))
    daily_used = int(data.get("daily_tokens_used", 0))
    monthly_limit = float(data.get("monthly_cost_limit", 50.00))
    monthly_used = float(data.get("monthly_cost_used", 0.00))
    rpm_limit = int(data.get("rate_limit_rpm", 60))
    tpm_limit = int(data.get("rate_limit_tpm", 100_000))

    # 计算重置时间
    now_utc = datetime.now(timezone.utc)
    daily_reset = now_utc.replace(hour=0, minute=0, second=0, microsecond=0) + __import__("datetime").timedelta(days=1)
    if now_utc.hour >= 0:
        daily_reset_str = daily_reset.strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        daily_reset_str = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    monthly_reset = now_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0) + __import__("datetime").timedelta(days=32)
    monthly_reset = monthly_reset.replace(day=1)
    monthly_reset_str = monthly_reset.strftime("%Y-%m-%dT%H:%M:%SZ")

    # 告警检查
    alerts: List[Dict[str, Any]] = []
    daily_pct = daily_used / daily_limit if daily_limit > 0 else 0
    monthly_pct = monthly_used / monthly_limit if monthly_limit > 0 else 0

    if daily_pct >= 0.8:
        alerts.append({
            "type": "budget_warning",
            "threshold_percent": 80,
            "message": f"Daily token usage has reached {daily_pct:.0%}",
        })
    if monthly_pct >= 0.8:
        alerts.append({
            "type": "budget_warning",
            "threshold_percent": 80,
            "message": f"Monthly cost usage has reached {monthly_pct:.0%}",
        })

    rpm_window_start = int(data.get("rpm_window_start", 0))
    rpm_current = int(data.get("rpm_window_count", 0))
    tpm_current = int(data.get("tpm_window_count", 0))

    return {
        "data": {
            "id": key_id,
            "user_id": data.get("user_id", ""),
            "status": data.get("status", "active"),
            "quotas": {
                "daily_tokens": {
                    "used": daily_used,
                    "limit": daily_limit,
                    "reset_at": daily_reset_str,
                },
                "monthly_cost": {
                    "used": round(monthly_used, 2),
                    "limit": monthly_limit,
                    "reset_at": monthly_reset_str,
                },
                "rate_limit": {
                    "rpm": {
                        "current": rpm_current,
                        "limit": rpm_limit,
                    },
                    "tpm": {
                        "current": tpm_current,
                        "limit": tpm_limit,
                    },
                },
            },
            "alerts": alerts,
            "last_request_at": data.get("last_used_at") or None,
            "total_requests_today": int(data.get("daily_tokens_used", 0)),
            "total_tokens_today": daily_used,
        },
        "message": "success",
    }


# ------------------------------------------------------------------
# GET/PUT /admin/global-config — 全局配置（热重载、调试模式）
# ------------------------------------------------------------------


@router.get("/global-config")
async def get_global_config(
    request: Request,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
):

    """返回全局配置（热重载、调试模式）。

    直接从文件读取，避免多 worker 内存不一致。
    """
    import os
    import yaml
    from aigateway_api.main import app
    s = app.state
    config_manager = getattr(s, "config_manager")

    hot_reload = False
    debug_mode = False

    if config_manager:
        config_path = config_manager.config_path
        if config_path and os.path.isfile(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                file_config = yaml.safe_load(f) or {}
            hot_reload = file_config.get("hot_reload", False)
            debug_mode = file_config.get("debug_mode", False)

    return {
        "data": {
            "hot_reload": hot_reload,
            "debug_mode": debug_mode,
        },
        "message": "success",
    }


@router.put("/global-config")
async def update_global_config(
    request: Request,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
):

    """更新全局配置（热重载、调试模式）。"""
    from aigateway_api.main import app
    s = app.state
    config_manager = getattr(s, "config_manager")
    import yaml

    if not config_manager:
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": "ConfigManager not initialized"}})

    raw = await request.json()
    hot_reload = raw.get("hot_reload", False)
    debug_mode = raw.get("debug_mode", False)

    # 更新内存缓存
    config_manager.set("hot_reload", hot_reload)
    config_manager.set("debug_mode", debug_mode)

    # 写回 config.yaml（只追加这两个键）
    config_path = config_manager.config_path
    if config_path:
        import os
        if os.path.isfile(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                file_config = yaml.safe_load(f) or {}
            file_config["hot_reload"] = hot_reload
            file_config["debug_mode"] = debug_mode
            writable_keys = {"server", "auth", "plugins", "providers", "embedding", "observability", "hot_reload", "debug_mode"}
            clean_config = {k: v for k, v in file_config.items() if k in writable_keys}
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(clean_config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    # 根据 hot_reload 开关启停 Watchdog
    if hot_reload:
        config_manager.start_watching()
    else:
        config_manager.stop_watching()

    # 根据 debug_mode 调整日志级别
    if debug_mode:
        from aigateway_core.logger import setup_logging
        setup_logging(log_level="DEBUG")

    return {
        "data": {"hot_reload": hot_reload, "debug_mode": debug_mode},
        "message": "success",
    }


# ------------------------------------------------------------------
# GET /admin/logs — 请求日志
# ------------------------------------------------------------------


@router.get("/logs")
async def get_request_logs(
    request: Request,
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=50, ge=1, le=200, description="每页数量"),
    user_id: Optional[str] = Query(default=None, description="按用户筛选"),
    model: Optional[str] = Query(default=None, description="按模型筛选"),
    status: Optional[str] = Query(default=None, description="按状态码筛选"),
    cache_only: bool = Query(default=False, description="仅缓存命中"),
    _auth: Dict[str, Any] = Depends(authenticate_admin),
):
    """从 Redis 查询最近的请求日志。"""
    from aigateway_api.main import app
    s = app.state
    redis_mgr = getattr(s, "redis_manager")

    if redis_mgr is None or redis_mgr.redis is None:
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": "Redis not connected"}})

    # 获取最近 page_size * 2 条（过滤后再分页）
    all_logs = await redis_mgr.redis.zrevrange("aigateway:logs:requests", 0, page_size * 2 - 1, withscores=True)

    results = []
    for raw, score in all_logs:
        entry = json.loads(raw.decode() if isinstance(raw, bytes) else raw)

        # 过滤
        if user_id and entry.get("user_id") != user_id:
            continue
        if model and entry.get("model") != model:
            continue
        if status and str(entry.get("status")) != status:
            continue
        if cache_only and not entry.get("cache_hit"):
            continue

        results.append({
            "request_id": entry["request_id"],
            "trace_id": entry["trace_id"],
            "user_id": entry["user_id"],
            "timestamp": entry["timestamp"],
            "method": entry["method"],
            "endpoint": entry["endpoint"],
            "model": entry["model"],
            "status": entry["status"],
            "duration_ms": entry["duration_ms"],
            "cache_hit": entry["cache_hit"],
            "tier": entry.get("tier"),
        })

        if len(results) >= page_size:
            break

    total = len(results)  # 简化：不返回总数，只返回当前页

    return {
        "data": {
            "items": results,
            "pagination": {
                "page": page,
                "pageSize": page_size,
                "total": total,
            },
        },
        "message": "success",
    }
