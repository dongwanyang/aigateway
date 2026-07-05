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

# 模块级 Embedding 模型缓存（避免每次请求重新加载）
_embedding_model_cache: dict = {}


def _get_embedding_model():
    """获取缓存的 embedding 模型实例。"""
    return _embedding_model_cache.get("model")


def _set_embedding_model(model):
    """缓存 embedding 模型实例。"""
    _embedding_model_cache["model"] = model


async def _compute_embeddings_via_litellm(texts: List[str]) -> Optional[List[List[float]]]:
    """使用 litellm 的 embedding API 计算向量（不需要本地模型）。

    尝试顺序:
    1. 使用配置中有效的 provider embedding
    2. 回退到简单的哈希向量（精度低但能工作）

    返回 1024 维向量列表，失败返回 None。
    """
    try:
        from aigateway_api.main import app
        s = app.state
        config_manager = getattr(s, "config_manager", None)

        # 获取 embedding 配置
        embedding_cfg = config_manager.get("embedding", {}) if config_manager else {}
        providers_cfg = config_manager.get("providers", {}) if config_manager else {}

        # 尝试使用配置中有真实 API Key 的 provider
        import litellm

        # 查找可能有效的 provider（排除明显的占位符 key）
        for provider_name, provider_cfg in providers_cfg.items():
            if not isinstance(provider_cfg, dict):
                continue
            api_key = provider_cfg.get("api_key", "")
            # 跳过占位符 key
            if not api_key or api_key.endswith("xxx") or len(api_key) < 20:
                continue

            base_url = provider_cfg.get("base_url", "")

            try:
                # 尝试使用 openai 兼容的 embedding 接口
                vectors = []
                batch_size = 50
                for i in range(0, len(texts), batch_size):
                    batch = texts[i:i + batch_size]
                    kwargs = {
                        "model": "openai/text-embedding-3-small",
                        "input": batch,
                        "api_key": api_key,
                    }
                    if base_url:
                        kwargs["api_base"] = base_url

                    response = await litellm.aembedding(**kwargs)
                    for item in response.data:
                        vec = item["embedding"]
                        # 调整维度到 1024
                        if len(vec) > 1024:
                            vec = vec[:1024]
                        elif len(vec) < 1024:
                            vec = vec + [0.0] * (1024 - len(vec))
                        vectors.append(vec)

                if vectors:
                    return vectors
            except Exception as provider_exc:
                logger.debug("Provider %s embedding 失败: %s", provider_name, provider_exc)
                continue

    except Exception as exc:
        logger.warning("litellm embedding 失败: %s", exc)

    # 最终回退：使用简单的哈希向量（精度低但功能可用）
    logger.info("使用哈希向量回退方案（embedding API 不可用）")
    return _compute_hash_embeddings(texts)


def _compute_hash_embeddings(texts: List[str]) -> List[List[float]]:
    """基于哈希的简单向量生成（作为 embedding 不可用时的回退方案）。

    使用 SHA-256 哈希生成伪随机 1024 维向量。
    注意：这不是真正的语义嵌入，只保证相同文本产生相同向量。
    """
    import hashlib
    import struct

    vectors = []
    for text in texts:
        # 多次哈希以生成足够的维度
        result = []
        for i in range(64):  # 64 * 16 = 1024 floats
            h = hashlib.sha256(f"{text}:{i}".encode()).digest()
            # 将 32 bytes 解释为 16 个 float16 值
            for j in range(0, 32, 2):
                val = struct.unpack('h', h[j:j+2])[0] / 32768.0  # 归一化到 [-1, 1]
                result.append(val)
        # 截断到 1024 维
        result = result[:1024]
        # L2 归一化
        norm = sum(x*x for x in result) ** 0.5
        if norm > 0:
            result = [x / norm for x in result]
        vectors.append(result)
    return vectors


router = APIRouter()


# ------------------------------------------------------------------
# 请求模型
# ------------------------------------------------------------------


class CreateApiKeyRequest(BaseModel):
    """POST /admin/api-keys 请求体。"""

    user_id: str = Field(..., min_length=1, description="关联的用户 ID")
    daily_tokens: Optional[int] = Field(default=None, description="每日 token 上限")
    monthly_cost: Optional[float] = Field(default=None, description="每月成本上限（美元）")
    rate_limit_rpm: Optional[int] = Field(default=None, description="每分钟请求数上限")
    rate_limit_tpm: Optional[int] = Field(default=None, description="每分钟 token 数上限")


class UpdateQuotaRequest(BaseModel):
    """PUT /admin/api-keys/{key_id} 请求体 — 修改用户配额。"""

    daily_tokens: Optional[int] = Field(default=None, ge=1, description="每日 token 上限")
    monthly_cost: Optional[float] = Field(default=None, gt=0, description="每月成本上限（美元）")
    rate_limit_rpm: Optional[int] = Field(default=None, ge=1, description="每分钟请求数上限")
    rate_limit_tpm: Optional[int] = Field(default=None, ge=1, description="每分钟 token 数上限")


# ------------------------------------------------------------------
# 辅助函数
# ------------------------------------------------------------------


def _get_keystore_and_metrics(request: Request) -> tuple[Any, Any]:
    """从 app.state 获取 KeyStore 和 MetricsCollector。"""
    from aigateway_api.main import app
    return getattr(app.state, "key_store"), getattr(app.state, "metrics_collector")


def _get_auth_defaults() -> Dict[str, Any]:
    """从 config 获取 auth.defaults 配额默认值。"""
    from aigateway_api.main import app
    config_manager = getattr(app.state, "config_manager", None)
    if config_manager:
        auth_cfg = config_manager.get("auth", {})
        defaults = auth_cfg.get("defaults", {}) if isinstance(auth_cfg, dict) else {}
        return {
            "daily_tokens": int(defaults.get("daily_tokens", 1_000_000)),
            "monthly_cost": float(defaults.get("monthly_cost", 50.0)),
            "rate_limit_rpm": int(defaults.get("rate_limit_rpm", 60)),
            "rate_limit_tpm": int(defaults.get("rate_limit_tpm", 100_000)),
        }
    return {
        "daily_tokens": 1_000_000,
        "monthly_cost": 50.0,
        "rate_limit_rpm": 60,
        "rate_limit_tpm": 100_000,
    }


def _get_budget_alert_threshold() -> float:
    """从 config 获取 auth.budget_alert_threshold。"""
    from aigateway_api.main import app
    config_manager = getattr(app.state, "config_manager", None)
    if config_manager:
        auth_cfg = config_manager.get("auth", {})
        return float(auth_cfg.get("budget_alert_threshold", 0.8)) if isinstance(auth_cfg, dict) else 0.8
    return 0.8


def _format_quota_item(key_data: Dict[str, Any], key_hash: str) -> Dict[str, Any]:
    """格式化单个 API Key 的配额信息。"""
    defaults = _get_auth_defaults()
    daily_limit = int(key_data.get("daily_tokens_limit", defaults["daily_tokens"]))
    daily_used = int(key_data.get("daily_tokens_used", 0))
    monthly_limit = float(key_data.get("monthly_cost_limit", defaults["monthly_cost"]))
    monthly_used = float(key_data.get("monthly_cost_used", 0.00))
    rpm_limit = int(key_data.get("rate_limit_rpm", defaults["rate_limit_rpm"]))
    tpm_limit = int(key_data.get("rate_limit_tpm", defaults["rate_limit_tpm"]))

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
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": "Redis connection required for key management"}})

    # Auto-reseed: 如果 Redis 中没有 API Key，自动从 config.yaml 重新导入
    from aigateway_api.main import app
    config_manager = getattr(app.state, "config_manager")
    if config_manager:
        auth_config = config_manager.get("auth", {})
        keys_config = auth_config.get("api_keys", [])
        await key_store.ensure_seeded(keys_config)

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
            if data and data.get("status") == "active":
                data["_key_hash"] = kh
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
    defaults = _get_auth_defaults()

    # 验证配额参数
    if body.daily_tokens is not None and body.daily_tokens <= 0:
        raise HTTPException(status_code=400, detail={"error": {"code": "validation_error", "message": "daily_tokens must be a positive integer"}})
    if body.monthly_cost is not None and body.monthly_cost <= 0:
        raise HTTPException(status_code=400, detail={"error": {"code": "validation_error", "message": "monthly_cost must be a positive number"}})

    quotas = {
        "daily_tokens": body.daily_tokens if body.daily_tokens is not None else defaults["daily_tokens"],
        "monthly_cost": body.monthly_cost if body.monthly_cost is not None else defaults["monthly_cost"],
        "rate_limit_rpm": body.rate_limit_rpm if body.rate_limit_rpm is not None else defaults["rate_limit_rpm"],
        "rate_limit_tpm": body.rate_limit_tpm if body.rate_limit_tpm is not None else defaults["rate_limit_tpm"],
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

    """删除指定的 API Key（从 Redis 中完全移除）。"""
    key_store, _ = _get_keystore_and_metrics(request)

    if not key_id.startswith("key_"):
        raise HTTPException(status_code=400, detail={"error": {"code": "validation_error", "message": "Invalid key_id format"}})

    success = await key_store.delete_permanently(key_id)
    if not success:
        raise HTTPException(status_code=404, detail={"error": {"code": "not_found", "message": f"API key '{key_id}' not found"}})

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "data": {
            "id": key_id,
            "status": "deleted",
            "deleted_at": now_iso,
        },
        "message": "success",
    }


# ------------------------------------------------------------------
# PUT /admin/api-keys/{key_id} — 修改用户配额
# ------------------------------------------------------------------


@router.put("/api-keys/{key_id}")
async def update_api_key_quota(
    request: Request,
    key_id: str,
    body: UpdateQuotaRequest,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
):
    """修改指定 API Key 的配额限制。

    仅更新请求中包含的字段（非 None 字段），其余保持不变。
    """
    key_store, _ = _get_keystore_and_metrics(request)
    redis_mgr = key_store.redis

    if redis_mgr is None or redis_mgr.redis is None:
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": "Redis not connected"}})

    if not key_id.startswith("key_"):
        raise HTTPException(status_code=400, detail={"error": {"code": "validation_error", "message": "Invalid key_id format"}})

    # 通过 key_id 查找 key_hash
    key_hashes = await key_store._find_key_hashes_by_id(key_id)
    if not key_hashes:
        raise HTTPException(status_code=404, detail={"error": {"code": "not_found", "message": f"API key '{key_id}' not found"}})

    kh = key_hashes[0]
    data = await redis_mgr.get_api_key(kh)
    if not data:
        raise HTTPException(status_code=404, detail={"error": {"code": "not_found", "message": f"API key '{key_id}' not found"}})

    # 仅更新非 None 字段
    updated_fields: Dict[str, str] = {}
    if body.daily_tokens is not None:
        updated_fields["daily_tokens_limit"] = str(body.daily_tokens)
    if body.monthly_cost is not None:
        updated_fields["monthly_cost_limit"] = str(body.monthly_cost)
    if body.rate_limit_rpm is not None:
        updated_fields["rate_limit_rpm"] = str(body.rate_limit_rpm)
    if body.rate_limit_tpm is not None:
        updated_fields["rate_limit_tpm"] = str(body.rate_limit_tpm)

    if not updated_fields:
        raise HTTPException(status_code=400, detail={"error": {"code": "validation_error", "message": "No fields to update"}})

    # 合并到现有数据并写回 Redis
    data.update(updated_fields)
    await redis_mgr.set_api_key(kh, data)

    # 通过 Pub/Sub 广播配额变更事件（多实例同步）
    try:
        pub_msg = key_store._build_pubsub_message(
            "quota_updated", key_id, data.get("user_id", ""),
            updated_fields=updated_fields,
        )
        await redis_mgr.publish(key_store.PUBSUB_CHANNEL, pub_msg)
    except Exception as exc:
        logger.warning("Failed to publish quota update event: %s", exc)

    logger.info("API Key 配额已更新: key_id=%s, fields=%s", key_id, list(updated_fields.keys()))

    return {
        "data": {
            "id": key_id,
            "user_id": data.get("user_id", ""),
            "quotas": {
                "daily_tokens_limit": int(data.get("daily_tokens_limit", _get_auth_defaults()["daily_tokens"])),
                "monthly_cost_limit": float(data.get("monthly_cost_limit", _get_auth_defaults()["monthly_cost"])),
                "rate_limit_rpm": int(data.get("rate_limit_rpm", _get_auth_defaults()["rate_limit_rpm"])),
                "rate_limit_tpm": int(data.get("rate_limit_tpm", _get_auth_defaults()["rate_limit_tpm"])),
            },
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
    # 从 registry 查每个插件的 pipeline_kind（注册时由代码设置，不在 YAML 里）
    registry = getattr(s, "plugin_registry", None)
    reg_map = {}
    if registry is not None:
        for name, reg in getattr(registry, "_registrations", {}).items():
            reg_map[name] = reg

    def _build_plugin_entry(p: dict) -> dict:
        name = p.get("name", "unknown")
        entry = {
            "name": name,
            "enabled": p.get("enabled", True),
            "depends_on": p.get("depends_on", []),
            "config": p.get("config", {}),
        }
        reg = reg_map.get(name)
        if reg is not None:
            entry["pipeline_kind"] = getattr(reg, "pipeline_kind", "understanding")
            entry["priority"] = getattr(reg, "priority", 0)
        return entry

    def _serializable_config(cfg: dict) -> dict:
        """过滤掉不可 JSON 序列化的 config 字段（如 strategy/tracker 实例）。

        生成管道插件的 config 含 strategy 对象（带 lock、不可序列化），
        admin 响应只展示可序列化的配置项。
        """
        out = {}
        for k, v in (cfg or {}).items():
            try:
                import json as _json
                _json.dumps(v)
                out[k] = v
            except (TypeError, ValueError):
                out[k] = f"<non-serializable: {type(v).__name__}>"
        return out

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
                        plugins.append(_build_plugin_entry(p))
        except Exception as exc:
            logger.warning("读取插件配置失败，回退到内存缓存: %s", exc)
            # 回退：从内存缓存读取
            plugins_cfg = config_manager.get("plugins", [])
            for p in plugins_cfg:
                if isinstance(p, dict):
                    plugins.append(_build_plugin_entry(p))

    # 补充 registry 里注册但 YAML plugins 段未列的插件（如 generation 管道 6 插件，
    # 它们由代码注册，YAML 里没有对应条目）。前端据此显示生成管道插件。
    seen_names = {p["name"] for p in plugins}
    if registry is not None:
        for name, reg in getattr(registry, "_registrations", {}).items():
            if name in seen_names:
                continue
            plugins.append({
                "name": name,
                "enabled": getattr(reg, "enabled", True),
                "depends_on": list(getattr(reg, "depends_on", []) or []),
                "config": _serializable_config(getattr(reg, "config", {}) or {}),
                "pipeline_kind": getattr(reg, "pipeline_kind", "understanding"),
                "priority": getattr(reg, "priority", 0),
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

        # 原子交换内存配置并触发热重载回调（重建插件实例等）。
        # 直接 _set_nested 只改内存不通知，导致插件 enabled 改动不生效——
        # 改用 atomic_swap 走标准的 swap + _notify_reload 流程。
        import copy
        new_config = copy.deepcopy(config_manager._config)
        config_manager._set_nested(new_config, "plugins", plugins_cfg)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()

    # 在锁外执行原子交换（atomic_swap 内部有自己的锁），触发 on_reload 回调
    config_manager.atomic_swap(new_config)

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

    defaults = _get_auth_defaults()
    budget_alert_threshold = _get_budget_alert_threshold()

    daily_limit = int(data.get("daily_tokens_limit", defaults["daily_tokens"]))
    daily_used = int(data.get("daily_tokens_used", 0))
    monthly_limit = float(data.get("monthly_cost_limit", defaults["monthly_cost"]))
    monthly_used = float(data.get("monthly_cost_used", 0.00))
    rpm_limit = int(data.get("rate_limit_rpm", defaults["rate_limit_rpm"]))
    tpm_limit = int(data.get("rate_limit_tpm", defaults["rate_limit_tpm"]))

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

    threshold_percent = int(budget_alert_threshold * 100)
    if daily_pct >= budget_alert_threshold:
        alerts.append({
            "type": "budget_warning",
            "threshold_percent": threshold_percent,
            "message": f"Daily token usage has reached {daily_pct:.0%}",
        })
    if monthly_pct >= budget_alert_threshold:
        alerts.append({
            "type": "budget_warning",
            "threshold_percent": threshold_percent,
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

    # 写回 config.yaml（只更新这两个键，保留其余 section 不变）
    config_path = config_manager.config_path
    if config_path:
        import os
        if os.path.isfile(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                file_config = yaml.safe_load(f) or {}
            # 仅覆写 admin 可编辑的键，不丢弃其他 section
            file_config["hot_reload"] = hot_reload
            file_config["debug_mode"] = debug_mode
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(file_config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    # 触发热重载回调（重建插件实例等）。config_manager.set 已改内存，
    # 但 set 不通知回调；这里显式 atomic_swap 走 _notify_reload。
    # 注意：set 已修改 _config，故 old==new，回调仍会被调用以重建插件。
    config_manager.atomic_swap(config_manager._config)

    # 根据 hot_reload 开关启停 Watchdog
    if hot_reload:
        config_manager.start_watching()
    else:
        config_manager.stop_watching()

    # 根据 debug_mode 调整日志级别（双向：开启时切 DEBUG，关闭时恢复原级别）
    if debug_mode:
        from aigateway_core.logger import setup_logging
        setup_logging(log_level="DEBUG")
    else:
        # 关闭调试模式时，恢复为 observability.log_level 配置的级别（默认 INFO）
        _obs = config_manager.get("observability") or {}
        _restore_level = (_obs.get("log_level", "info") if isinstance(_obs, dict) else "info")
        from aigateway_core.logger import setup_logging
        setup_logging(log_level=_restore_level.upper())

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
    """从 Redis 查询最近的请求日志（支持服务端分页）。"""
    from aigateway_api.main import app
    s = app.state
    redis_mgr = getattr(s, "redis_manager")

    if redis_mgr is None or redis_mgr.redis is None:
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": "Redis not connected"}})

    # 获取总数
    total_count = await redis_mgr.redis.zcard("aigateway:logs:requests")

    # 如果有过滤条件，获取更多条目进行过滤；否则精确分页
    has_filters = bool(user_id or model or status or cache_only)

    if has_filters:
        # 有过滤条件时，获取最近 500 条然后过滤
        all_logs = await redis_mgr.redis.zrevrange("aigateway:logs:requests", 0, 499, withscores=True)
        filtered = []
        for raw, score in all_logs:
            entry = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            if user_id and entry.get("user_id") != user_id:
                continue
            if model and entry.get("model") != model:
                continue
            if status and str(entry.get("status")) != status:
                continue
            if cache_only and not entry.get("cache_hit"):
                continue
            filtered.append(entry)

        total_filtered = len(filtered)
        start = (page - 1) * page_size
        end = start + page_size
        results = filtered[start:end]
    else:
        # 无过滤条件，直接 Redis 分页
        start = (page - 1) * page_size
        end = start + page_size - 1
        all_logs = await redis_mgr.redis.zrevrange("aigateway:logs:requests", start, end, withscores=True)
        results = []
        for raw, score in all_logs:
            entry = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            results.append(entry)
        total_filtered = total_count

    items = []
    for entry in results:
        items.append({
            "request_id": entry.get("request_id", ""),
            "trace_id": entry.get("trace_id", ""),
            "user_id": entry.get("user_id", ""),
            "timestamp": entry.get("timestamp", 0),
            "method": entry.get("method", ""),
            "endpoint": entry.get("endpoint", ""),
            "model": entry.get("model", ""),
            "status": entry.get("status", 0),
            "duration_ms": entry.get("duration_ms", 0),
            "cache_hit": entry.get("cache_hit", False),
            "tier": entry.get("tier"),
            "plugin_trace": entry.get("plugin_trace", []),
        })

    return {
        "data": {
            "items": items,
            "pagination": {
                "page": page,
                "pageSize": page_size,
                "total": total_filtered,
            },
        },
        "message": "success",
    }


# ------------------------------------------------------------------
# GET /admin/trace/:trace_id — 全链路追踪详情
# ------------------------------------------------------------------


@router.get("/trace/{trace_id}")
async def get_trace_detail(
    request: Request,
    trace_id: str,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
):
    """根据 trace_id 查询该请求的全链路信息（包括插件执行步骤）。

    优先读新通道 `aigateway:trace:{trace_id}`(TraceCollector.flush 写入,含完整
    kind=stage/plugin/debug 事件流);未命中时 fallback 到旧 ZSET 扫描(过渡期兼容)。
    响应始终包含 `events` 数组(新)+ `plugin_trace` 数组(旧字段,filter kind=plugin
    构建,供旧前端兼容,PR3 前端切换完成后 Task 21 会删)。
    """
    from aigateway_api.main import app
    s = app.state
    redis_mgr = getattr(s, "redis_manager")

    if redis_mgr is None or redis_mgr.redis is None:
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": "Redis not connected"}})

    # ---- 新通道: aigateway:trace:{trace_id} hash ----
    key = f"aigateway:trace:{trace_id}"
    raw = await redis_mgr.redis.hget(key, "data")
    if raw:
        data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        events = data.get("events", []) or []
        # 兼容字段:plugin_trace = events 中 kind==plugin 的子集
        plugin_trace = [
            {
                "plugin_name": e.get("stage"),
                "duration_ms": e.get("duration_ms"),
                "status": e.get("status"),
            }
            for e in events if e.get("kind") == "plugin"
        ]
        return {
            "data": {
                "trace_id": trace_id,
                "events": events,
                "plugin_trace": plugin_trace,  # 兼容旧前端,PR3 收尾时删
                "meta": {"wall_start": data.get("wall_start")},
            },
            "message": "success",
        }

    # ---- Fallback: 旧 ZSET 扫描(TraceMiddleware 未启动或 flush 前请求)----
    all_logs = await redis_mgr.redis.zrevrange("aigateway:logs:requests", 0, 999, withscores=True)
    matched = []
    for raw_entry, score in all_logs:
        entry = json.loads(raw_entry.decode() if isinstance(raw_entry, bytes) else raw_entry)
        if entry.get("trace_id") == trace_id:
            matched.append(entry)

    if not matched:
        raise HTTPException(status_code=404, detail={"error": {"code": "not_found", "message": f"Trace {trace_id} not found"}})

    primary = matched[0]
    trace_detail = {
        "trace_id": trace_id,
        "request_id": primary.get("request_id", ""),
        "user_id": primary.get("user_id", ""),
        "model": primary.get("model", ""),
        "endpoint": primary.get("endpoint", ""),
        "status": primary.get("status", 0),
        "duration_ms": primary.get("duration_ms", 0),
        "cache_hit": primary.get("cache_hit", False),
        "cache_tier": primary.get("tier"),
        "timestamp": primary.get("timestamp", 0),
        "events": [],  # 旧 ZSET 无 events;仅 plugin_trace 提供插件耗时
        "plugin_trace": primary.get("plugin_trace", []),
        "related_requests": matched[1:] if len(matched) > 1 else [],
    }

    return {
        "data": trace_detail,
        "message": "success",
    }


# ------------------------------------------------------------------
# DELETE /admin/logs — 清空请求日志
# ------------------------------------------------------------------


@router.delete("/logs")
async def delete_all_logs(
    request: Request,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
):
    """清空所有请求日志。"""
    from aigateway_api.main import app
    s = app.state
    redis_mgr = getattr(s, "redis_manager")

    if redis_mgr is None or redis_mgr.redis is None:
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": "Redis not connected"}})

    deleted_count = await redis_mgr.redis.delete("aigateway:logs:requests")

    return {
        "data": {"deleted": bool(deleted_count)},
        "message": "success",
    }

# ------------------------------------------------------------------
# GET/PUT /admin/config — 完整配置编辑 (Req 15)
# ------------------------------------------------------------------


@router.get("/config")
async def get_full_config(
    request: Request,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
):
    """返回当前 config.yaml 的完整内容（脱敏 API Key）。"""
    import os
    import yaml
    from aigateway_api.main import app
    s = app.state
    config_manager = getattr(s, "config_manager")

    if not config_manager:
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": "ConfigManager not initialized"}})

    config_path = config_manager.config_path
    if not config_path or not os.path.isfile(config_path):
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": "Config file not found"}})

    with open(config_path, "r", encoding="utf-8") as f:
        file_config = yaml.safe_load(f) or {}

    # 脱敏 providers 中的 api_key（只返回前 8 位 + ***）
    safe_config = json.loads(json.dumps(file_config, default=str))
    if "providers" in safe_config:
        for provider_name, provider_cfg in safe_config["providers"].items():
            if isinstance(provider_cfg, dict) and "api_key" in provider_cfg:
                key_val = provider_cfg["api_key"]
                if isinstance(key_val, str) and len(key_val) > 8 and not key_val.startswith("${"):
                    provider_cfg["api_key"] = key_val[:8] + "***"

    return {
        "data": safe_config,
        "message": "success",
    }


@router.put("/config")
async def update_full_config(
    request: Request,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
):
    """更新 config.yaml 配置（部分更新，仅支持安全字段）。"""
    import fcntl
    import os
    import yaml
    from aigateway_api.main import app
    s = app.state
    config_manager = getattr(s, "config_manager")

    if not config_manager:
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": "ConfigManager not initialized"}})

    config_path = config_manager.config_path
    if not config_path or not os.path.isfile(config_path):
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": "Config file not found"}})

    try:
        new_config = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": {"code": "validation_error", "message": "Invalid JSON body"}})

    # 安全字段白名单（不允许通过 API 修改 auth.api_keys 中的密钥明文）
    writable_keys = {"server", "plugins", "providers", "embedding", "observability", "infrastructure", "hot_reload", "debug_mode"}

    lock_path = config_path + ".lock"
    lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        with open(config_path, "r", encoding="utf-8") as f:
            file_config = yaml.safe_load(f) or {}

        # 合并更新（只更新白名单内的字段）
        for key in writable_keys:
            if key in new_config:
                # 对 providers，保留原始 api_key（前端传来的是脱敏的）
                if key == "providers" and isinstance(new_config[key], dict):
                    for pname, pcfg in new_config[key].items():
                        if isinstance(pcfg, dict) and "api_key" in pcfg:
                            if pcfg["api_key"].endswith("***"):
                                # 保留原始 key
                                orig = file_config.get("providers", {}).get(pname, {})
                                pcfg["api_key"] = orig.get("api_key", pcfg["api_key"])
                file_config[key] = new_config[key]

        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(file_config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

        # 触发热重载
        config_manager.load()

    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()

    return {
        "data": {"updated": True},
        "message": "success",
    }


# ------------------------------------------------------------------
# RAG 知识库管理 (Req 18)
# ------------------------------------------------------------------


@router.get("/rag/documents")
async def list_rag_documents(
    request: Request,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
):
    """列出已导入的 RAG 文档。"""
    from aigateway_api.main import app
    s = app.state
    redis_mgr = getattr(s, "redis_manager")

    if redis_mgr is None or redis_mgr.redis is None:
        return {"data": {"documents": []}, "message": "success"}

    # 从 Redis 获取文档元数据列表
    raw = await redis_mgr.redis.lrange("aigateway:rag:documents", 0, -1)
    documents = []
    for item in raw:
        try:
            doc = json.loads(item.decode() if isinstance(item, bytes) else item)
            documents.append(doc)
        except Exception:
            continue

    return {
        "data": {"documents": documents},
        "message": "success",
    }


@router.post("/rag/documents")
async def import_rag_document(
    request: Request,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
):
    """导入文档到 RAG 知识库。

    支持两种方式:
    - JSON body: {"url": "https://...", "chunk_strategy": "paragraph", "chunk_size": 512, "chunk_overlap": 64}
    - JSON body: {"content": "文本内容", "filename": "doc.txt", ...}
    """
    import time as time_mod
    import uuid
    import hashlib

    from aigateway_api.main import app
    s = app.state
    redis_mgr = getattr(s, "redis_manager")
    qdrant_mgr = getattr(s, "qdrant_manager")

    if qdrant_mgr is None or qdrant_mgr._http is None:
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": "Qdrant not connected"}})

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": {"code": "validation_error", "message": "Invalid JSON body"}})

    url = body.get("url")
    content = body.get("content", "")
    filename = body.get("filename", "")
    chunk_strategy = body.get("chunk_strategy", "fixed_size")  # paragraph | fixed_size | sentence
    chunk_size = int(body.get("chunk_size", 512))
    chunk_overlap = int(body.get("chunk_overlap", 64))

    start_time = time_mod.time()

    # 获取文本内容
    if url:
        # 抓取网页内容
        try:
            import httpx
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                html = resp.text

            # 简单提取正文（去除 HTML 标签）
            import re as re_mod
            # 移除 script/style 标签
            html = re_mod.sub(r'<script[^>]*>.*?</script>', '', html, flags=re_mod.DOTALL | re_mod.IGNORECASE)
            html = re_mod.sub(r'<style[^>]*>.*?</style>', '', html, flags=re_mod.DOTALL | re_mod.IGNORECASE)
            # 移除所有标签
            content = re_mod.sub(r'<[^>]+>', ' ', html)
            # 清理空白
            content = re_mod.sub(r'\s+', ' ', content).strip()
            filename = filename or url.split("/")[-1][:50] or "webpage"
        except Exception as exc:
            raise HTTPException(status_code=400, detail={"error": {"code": "validation_error", "message": f"Failed to fetch URL: {exc}"}})

    if not content:
        raise HTTPException(status_code=400, detail={"error": {"code": "validation_error", "message": "No content provided (use 'url' or 'content' field)"}})

    # 分块
    chunks = _split_text(content, strategy=chunk_strategy, chunk_size=chunk_size, overlap=chunk_overlap)

    if not chunks:
        raise HTTPException(status_code=400, detail={"error": {"code": "validation_error", "message": "Content too short to create chunks"}})

    # 计算 embeddings 并存入 Qdrant
    doc_id = f"doc_{uuid.uuid4().hex[:8]}"
    total_tokens = 0
    stored_count = 0

    # 确定 embedding 方式: sentence-transformers (本地) 或 litellm (远程 API)
    use_local_embedding = False
    try:
        from sentence_transformers import SentenceTransformer
        use_local_embedding = True
    except ImportError:
        pass

    try:
        # 确保 rag_documents 集合存在
        try:
            await qdrant_mgr.upsert_collection(name="rag_documents", size=1024, distance="COSINE")
        except Exception as coll_exc:
            logger.warning("确认 rag_documents 集合时出错（可能已存在）: %s", coll_exc)

        if use_local_embedding:
            # 使用本地 sentence-transformers 模型 (Qwen3-Embedding-0.6B)
            st_model = _get_embedding_model()
            if st_model is None:
                # 从配置读取模型名，默认使用 Qwen3-Embedding-0.6B
                from aigateway_api.main import app
                _cfg_mgr = getattr(app.state, "config_manager", None)
                _emb_cfg = _cfg_mgr.get("embedding", {}) if _cfg_mgr else {}
                _model_name = _emb_cfg.get("model", "Qwen/Qwen3-Embedding-0.6B")
                st_model = SentenceTransformer(_model_name)
                _set_embedding_model(st_model)

            # 批量 encode
            vectors = st_model.encode(chunks, normalize_embeddings=True, show_progress_bar=False)
            vectors_list = [v.tolist() for v in vectors]
        else:
            # 回退方案：使用 litellm embedding API 或哈希向量
            vectors_list = await _compute_embeddings_via_litellm(chunks)

        for i, (chunk_text, vector) in enumerate(zip(chunks, vectors_list)):
            point_id = str(uuid.uuid4())
            payload = {
                "document_id": doc_id,
                "filename": filename,
                "chunk_index": i,
                "chunk_text": chunk_text,
                "user_id": "admin",
                "created_at": int(time_mod.time()),
                "deleted": False,
            }
            payload_body = {"points": [{"id": point_id, "vector": vector, "payload": payload}]}
            resp = await qdrant_mgr._http.put(
                "/collections/rag_documents/points",
                json=payload_body,
            )
            resp.raise_for_status()
            stored_count += 1
            total_tokens += len(chunk_text) // 4

    except ImportError as exc:
        # This catches ImportError from model loading or sub-dependencies
        logger.error("RAG 导入依赖缺失: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": f"Missing dependency during embedding: {exc}"}})
    except Exception as exc:
        logger.error("RAG embedding 存储失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": f"Failed to store embeddings: {exc}"}})

    elapsed_ms = round((time_mod.time() - start_time) * 1000, 1)

    # 保存文档元数据到 Redis
    doc_meta = {
        "doc_id": doc_id,
        "filename": filename,
        "file_type": "url" if url else "text",
        "chunk_count": stored_count,
        "chunk_strategy": chunk_strategy,
        "chunk_size": chunk_size,
        "chunk_overlap": chunk_overlap,
        "total_tokens": total_tokens,
        "created_at": int(time_mod.time()),
        "url": url or "",
    }

    if redis_mgr and redis_mgr.redis:
        await redis_mgr.redis.rpush("aigateway:rag:documents", json.dumps(doc_meta))

    return {
        "data": {
            "doc_id": doc_id,
            "filename": filename,
            "chunk_count": stored_count,
            "total_tokens": total_tokens,
            "elapsed_ms": elapsed_ms,
        },
        "message": "success",
    }


@router.delete("/rag/documents/{doc_id}")
async def delete_rag_document(
    request: Request,
    doc_id: str,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
):
    """删除指定 RAG 文档及其在 Qdrant 中的所有向量。"""
    from aigateway_api.main import app
    s = app.state
    redis_mgr = getattr(s, "redis_manager")
    qdrant_mgr = getattr(s, "qdrant_manager")

    if qdrant_mgr is None or qdrant_mgr._http is None:
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": "Qdrant not connected"}})

    # 从 Qdrant 删除所有匹配 document_id 的 points
    try:
        delete_payload = {
            "filter": {
                "must": [
                    {"key": "document_id", "match": {"value": doc_id}}
                ]
            }
        }
        resp = await qdrant_mgr._http.post(
            "/collections/rag_documents/points/delete",
            json=delete_payload,
        )
        resp.raise_for_status()
    except Exception as exc:
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": f"Failed to delete from Qdrant: {exc}"}})

    # 从 Redis 移除文档元数据
    if redis_mgr and redis_mgr.redis:
        raw_list = await redis_mgr.redis.lrange("aigateway:rag:documents", 0, -1)
        for item in raw_list:
            try:
                doc = json.loads(item.decode() if isinstance(item, bytes) else item)
                if doc.get("doc_id") == doc_id:
                    await redis_mgr.redis.lrem("aigateway:rag:documents", 1, item)
                    break
            except Exception:
                continue

    return {
        "data": {"doc_id": doc_id, "deleted": True},
        "message": "success",
    }


# ------------------------------------------------------------------
# 文本分块辅助函数
# ------------------------------------------------------------------


def _split_text(text: str, strategy: str = "fixed_size", chunk_size: int = 512, overlap: int = 64) -> List[str]:
    """将文本按策略分块。

    Args:
        text: 原始文本。
        strategy: 分块策略 - "fixed_size" | "paragraph" | "sentence"
        chunk_size: 每块最大字符数。
        overlap: 相邻块重叠字符数。

    Returns:
        文本块列表。
    """
    if not text or len(text) < 10:
        return []

    if strategy == "paragraph":
        # 按段落分割（双换行符）
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        chunks = []
        current = ""
        for para in paragraphs:
            if len(current) + len(para) + 2 > chunk_size and current:
                chunks.append(current)
                # 重叠：保留当前块末尾部分
                current = current[-overlap:] + "\n\n" + para if overlap > 0 else para
            else:
                current = current + "\n\n" + para if current else para
        if current:
            chunks.append(current)
        return chunks

    elif strategy == "sentence":
        # 按句子分割
        import re as re_mod
        sentences = re_mod.split(r'(?<=[.!?。！？])\s+', text)
        chunks = []
        current = ""
        for sent in sentences:
            if len(current) + len(sent) + 1 > chunk_size and current:
                chunks.append(current)
                current = current[-overlap:] + " " + sent if overlap > 0 else sent
            else:
                current = current + " " + sent if current else sent
        if current:
            chunks.append(current)
        return chunks

    else:
        # fixed_size: 按固定字符数分块
        chunks = []
        start = 0
        while start < len(text):
            end = start + chunk_size
            chunk = text[start:end]
            if chunk.strip():
                chunks.append(chunk.strip())
            start = end - overlap if overlap > 0 else end
        return chunks


# ------------------------------------------------------------------
# L3 Cache Lifecycle Management (Design §9b)
# ------------------------------------------------------------------


@router.get("/cache/l3/config")
async def get_l3_cache_config(
    request: Request,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
):
    """返回当前 L3 缓存管理配置。"""
    import os
    import yaml
    from aigateway_api.main import app
    s = app.state
    config_manager = getattr(s, "config_manager")

    # 从 config.yaml 读取 cache.l3 配置
    default_config = {
        "default_mode": "auto",
        "auto_cleanup_interval_minutes": 60,
        "default_ttl_hours": 24,
        "min_ttl_hours": 1,
        "max_ttl_hours": 720,
    }

    if config_manager:
        cache_cfg = config_manager.get("cache", {})
        if isinstance(cache_cfg, dict):
            l3_cfg = cache_cfg.get("l3", {})
            if isinstance(l3_cfg, dict):
                default_config.update({
                    k: v for k, v in l3_cfg.items() if k in default_config
                })

    return {
        "data": default_config,
        "message": "success",
    }


@router.put("/cache/l3/config")
async def update_l3_cache_config(
    request: Request,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
):
    """更新 L3 缓存配置并持久化到 config.yaml。"""
    import yaml
    from aigateway_api.main import app
    s = app.state
    config_manager = getattr(s, "config_manager")

    if not config_manager:
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": "ConfigManager not initialized"}})

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": {"code": "validation_error", "message": "Invalid JSON body"}})

    # 验证配置值
    allowed_keys = {"default_mode", "auto_cleanup_interval_minutes", "default_ttl_hours", "min_ttl_hours", "max_ttl_hours"}
    l3_config = {}
    for key in allowed_keys:
        if key in body:
            l3_config[key] = body[key]

    if "default_mode" in l3_config and l3_config["default_mode"] not in ("auto", "manual"):
        raise HTTPException(status_code=400, detail={"error": {"code": "validation_error", "message": "default_mode must be 'auto' or 'manual'"}})

    # 更新内存缓存
    current_cache = config_manager.get("cache", {}) or {}
    if not isinstance(current_cache, dict):
        current_cache = {}
    current_l3 = current_cache.get("l3", {})
    if not isinstance(current_l3, dict):
        current_l3 = {}
    current_l3.update(l3_config)
    current_cache["l3"] = current_l3
    config_manager.set("cache", current_cache)

    # 更新清理调度器间隔
    if "auto_cleanup_interval_minutes" in l3_config:
        l3_scheduler = getattr(s, "l3_cleanup_scheduler", None)
        if l3_scheduler:
            l3_scheduler.update_interval(l3_config["auto_cleanup_interval_minutes"])

    return {
        "data": current_l3,
        "message": "success",
    }


@router.get("/cache/l3/entries")
async def list_l3_entries(
    request: Request,
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页数量"),
    mode: Optional[str] = Query(default=None, description="按模式过滤: auto | manual"),
    user_id: Optional[str] = Query(default=None, description="按用户过滤"),
    sort_by: str = Query(default="created_at", description="排序字段"),
    _auth: Dict[str, Any] = Depends(authenticate_admin),
):
    """列出 L3 缓存条目，支持分页、按模式和用户过滤。"""
    from aigateway_api.main import app
    s = app.state
    qdrant_mgr = getattr(s, "qdrant_manager")

    if qdrant_mgr is None or qdrant_mgr._http is None:
        return {
            "data": {"items": [], "pagination": {"page": page, "pageSize": page_size, "total": 0}},
            "message": "success",
        }

    # 构建过滤条件
    filter_conditions = []
    if mode:
        filter_conditions.append({"key": "management_mode", "match": {"value": mode}})
    if user_id:
        filter_conditions.append({"key": "user_id", "match": {"value": user_id}})

    qdrant_filter = {"must": filter_conditions} if filter_conditions else None

    try:
        result = await qdrant_mgr.scroll_points(
            collection="semantic_cache",
            filter=qdrant_filter,
            limit=page_size * page,  # 获取足够多的点进行分页
            with_payload=True,
        )
        all_points = result.get("points", [])
    except Exception as exc:
        logger.warning("L3 entries 查询失败: %s", exc)
        return {
            "data": {"items": [], "pagination": {"page": page, "pageSize": page_size, "total": 0}},
            "message": "success",
        }

    # 排序
    if sort_by == "hit_count":
        all_points.sort(key=lambda p: p.get("payload", {}).get("hit_count", 0), reverse=True)
    elif sort_by == "expires_at":
        all_points.sort(key=lambda p: p.get("payload", {}).get("ttl", 0), reverse=True)
    else:  # created_at
        all_points.sort(key=lambda p: p.get("payload", {}).get("created_at", 0), reverse=True)

    # 分页
    total = len(all_points)
    start = (page - 1) * page_size
    end = start + page_size
    paginated = all_points[start:end]

    items = []
    for point in paginated:
        payload = point.get("payload", {})
        prompt_normalized = payload.get("prompt_normalized", "")
        items.append({
            "id": point.get("id", ""),
            "promptPreview": prompt_normalized[:100] if prompt_normalized else "",
            "model": payload.get("model", ""),
            "userId": payload.get("user_id", ""),
            "createdAt": payload.get("created_at", 0),
            "expiresAt": payload.get("ttl", 0) if payload.get("management_mode") == "auto" else None,
            "mode": payload.get("management_mode", "auto"),
            "hitCount": payload.get("hit_count", 0),
            "tokenCount": payload.get("token_count", 0),
        })

    return {
        "data": {
            "items": items,
            "pagination": {"page": page, "pageSize": page_size, "total": total},
        },
        "message": "success",
    }


@router.put("/cache/l3/entries/{point_id}/mode")
async def update_entry_mode(
    request: Request,
    point_id: str,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
):
    """切换缓存条目的管理模式。

    auto → manual: 清除 TTL（设为 0，表示永不过期）
    manual → auto: 按 ttl_hours 设置过期时间
    """
    import time as time_mod
    from aigateway_api.main import app
    s = app.state
    qdrant_mgr = getattr(s, "qdrant_manager")
    config_manager = getattr(s, "config_manager")

    if qdrant_mgr is None or qdrant_mgr._http is None:
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": "Qdrant not connected"}})

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": {"code": "validation_error", "message": "Invalid JSON body"}})

    new_mode = body.get("mode")
    ttl_hours = body.get("ttl_hours")

    if new_mode not in ("auto", "manual"):
        raise HTTPException(status_code=400, detail={"error": {"code": "validation_error", "message": "mode must be 'auto' or 'manual'"}})

    # 构建 payload 更新
    now = int(time_mod.time())
    update_payload: Dict[str, Any] = {"management_mode": new_mode}

    if new_mode == "manual":
        update_payload["ttl"] = 0  # 永不过期
    else:
        # auto 模式：计算新的 TTL
        if ttl_hours is None:
            # 使用全局默认值
            cache_cfg = config_manager.get("cache", {}) if config_manager else {}
            l3_cfg = cache_cfg.get("l3", {}) if isinstance(cache_cfg, dict) else {}
            ttl_hours = l3_cfg.get("default_ttl_hours", 24) if isinstance(l3_cfg, dict) else 24
        update_payload["ttl"] = now + int(ttl_hours) * 3600

    try:
        await qdrant_mgr.update_payload(
            collection="semantic_cache",
            point_id=point_id,
            payload=update_payload,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": f"Failed to update: {exc}"}})

    return {
        "data": {"point_id": point_id, "mode": new_mode, "ttl": update_payload.get("ttl", 0)},
        "message": "success",
    }


@router.delete("/cache/l3/entries/{point_id}")
async def delete_l3_entry(
    request: Request,
    point_id: str,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
):
    """手动删除指定的 L3 缓存条目（任何模式均可删除）。"""
    from aigateway_api.main import app
    s = app.state
    qdrant_mgr = getattr(s, "qdrant_manager")

    if qdrant_mgr is None or qdrant_mgr._http is None:
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": "Qdrant not connected"}})

    try:
        await qdrant_mgr.delete_points(
            collection="semantic_cache",
            point_ids=[point_id],
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": f"Failed to delete: {exc}"}})

    return {
        "data": {"point_id": point_id, "deleted": True},
        "message": "success",
    }


@router.post("/cache/l3/cleanup")
async def trigger_l3_cleanup(
    request: Request,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
):
    """手动触发一次 L3 过期清理（只清理 mode=auto 且已过期的条目）。"""
    from aigateway_api.main import app
    s = app.state
    cache_manager = getattr(s, "cache_manager")

    if cache_manager is None:
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": "CacheManager not initialized"}})

    deleted = await cache_manager.cleanup_expired_l3()

    return {
        "data": {"deleted_count": deleted},
        "message": "success",
    }


# ------------------------------------------------------------------
# POST /admin/providers/{provider}/test — 提供商连通性测试
# ------------------------------------------------------------------


@router.post("/providers/{provider}/test")
async def test_provider_connectivity(
    request: Request,
    provider: str,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
):
    """测试指定提供商的 API 连通性。

    发送一个轻量请求（models list 或简单 completion）来验证 API Key 和网络是否可用。
    """
    import os
    import time as time_mod
    import yaml

    from aigateway_api.main import app
    s = app.state
    config_manager = getattr(s, "config_manager")

    if not config_manager:
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": "ConfigManager not initialized"}})

    # 从配置文件读取 provider 信息
    config_path = config_manager.config_path
    if not config_path or not os.path.isfile(config_path):
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": "Config file not found"}})

    with open(config_path, "r", encoding="utf-8") as f:
        file_config = yaml.safe_load(f) or {}

    providers_cfg = file_config.get("providers", {})
    if provider not in providers_cfg:
        raise HTTPException(status_code=404, detail={"error": {"code": "not_found", "message": f"Provider '{provider}' not found in config"}})

    provider_cfg = providers_cfg[provider]
    api_key = provider_cfg.get("api_key", "")
    base_url = provider_cfg.get("base_url", "")

    # 确定 base_url
    if not base_url:
        default_urls = {
            "openai": "https://api.openai.com/v1",
            "anthropic": "https://api.anthropic.com/v1",
            "google": "https://generativelanguage.googleapis.com/v1beta/openai",
            "deepseek": "https://api.deepseek.com/v1",
            "zhipu": "https://open.bigmodel.cn/api/paas/v4",
            "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "moonshot": "https://api.moonshot.cn/v1",
            "doubao": "https://ark.cn-beijing.volces.com/api/v3",
            "yi": "https://api.lingyiwanwu.com/v1",
            "minimax": "https://api.minimax.chat/v1",
            "groq": "https://api.groq.com/openai/v1",
            "mistral": "https://api.mistral.ai/v1",
            "openrouter": "https://openrouter.ai/api/v1",
            "siliconflow": "https://api.siliconflow.cn/v1",
        }
        base_url = default_urls.get(provider, "")

    if not base_url:
        return {
            "data": {
                "provider": provider,
                "success": False,
                "latency_ms": 0,
                "error": "No base_url configured for this provider",
            },
            "message": "success",
        }

    start = time_mod.time()
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as client:
            # 尝试调用 /models 端点
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            resp = await client.get(f"{base_url}/models", headers=headers)
            latency_ms = round((time_mod.time() - start) * 1000, 1)

            if resp.status_code < 400:
                return {
                    "data": {
                        "provider": provider,
                        "success": True,
                        "latency_ms": latency_ms,
                    },
                    "message": "success",
                }
            else:
                return {
                    "data": {
                        "provider": provider,
                        "success": False,
                        "latency_ms": latency_ms,
                        "error": f"HTTP {resp.status_code}: {resp.text[:200]}",
                    },
                    "message": "success",
                }
    except Exception as exc:
        latency_ms = round((time_mod.time() - start) * 1000, 1)
        return {
            "data": {
                "provider": provider,
                "success": False,
                "latency_ms": latency_ms,
                "error": str(exc)[:300],
            },
            "message": "success",
        }


# ------------------------------------------------------------------
# GET /admin/providers/{provider}/models — 获取提供商可用模型列表
# ------------------------------------------------------------------


@router.get("/providers/{provider}/models")
async def get_provider_models(
    request: Request,
    provider: str,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
):
    """从提供商 API 获取可用的模型列表。"""
    import os
    import yaml

    from aigateway_api.main import app
    s = app.state
    config_manager = getattr(s, "config_manager")

    if not config_manager:
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": "ConfigManager not initialized"}})

    config_path = config_manager.config_path
    if not config_path or not os.path.isfile(config_path):
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": "Config file not found"}})

    with open(config_path, "r", encoding="utf-8") as f:
        file_config = yaml.safe_load(f) or {}

    providers_cfg = file_config.get("providers", {})
    if provider not in providers_cfg:
        raise HTTPException(status_code=404, detail={"error": {"code": "not_found", "message": f"Provider '{provider}' not found in config"}})

    provider_cfg = providers_cfg[provider]
    api_key = provider_cfg.get("api_key", "")
    base_url = provider_cfg.get("base_url", "")

    if not base_url:
        default_urls = {
            "openai": "https://api.openai.com/v1",
            "anthropic": "https://api.anthropic.com/v1",
            "google": "https://generativelanguage.googleapis.com/v1beta/openai",
            "deepseek": "https://api.deepseek.com/v1",
            "zhipu": "https://open.bigmodel.cn/api/paas/v4",
            "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "moonshot": "https://api.moonshot.cn/v1",
            "doubao": "https://ark.cn-beijing.volces.com/api/v3",
            "yi": "https://api.lingyiwanwu.com/v1",
            "minimax": "https://api.minimax.chat/v1",
            "groq": "https://api.groq.com/openai/v1",
            "mistral": "https://api.mistral.ai/v1",
            "openrouter": "https://openrouter.ai/api/v1",
            "siliconflow": "https://api.siliconflow.cn/v1",
        }
        base_url = default_urls.get(provider, "")

    if not base_url:
        raise HTTPException(status_code=400, detail={"error": {"code": "validation_error", "message": f"No base_url configured for provider '{provider}'"}})

    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as client:
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            resp = await client.get(f"{base_url}/models", headers=headers)
            resp.raise_for_status()
            data = resp.json()

            # OpenAI 兼容格式: {"data": [{"id": "gpt-4o", ...}]}
            models = []
            if isinstance(data, dict) and "data" in data:
                for m in data["data"]:
                    if isinstance(m, dict) and "id" in m:
                        models.append(m["id"])
            elif isinstance(data, list):
                for m in data:
                    if isinstance(m, dict) and "id" in m:
                        models.append(m["id"])
                    elif isinstance(m, str):
                        models.append(m)

            models.sort()
            return {
                "data": {
                    "provider": provider,
                    "models": models,
                },
                "message": "success",
            }
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"error": {"code": "upstream_error", "message": f"Failed to fetch models from {provider}: {exc}"}})
