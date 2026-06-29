"""
aigateway-api 应用入口
=====================

创建 FastAPI 实例，挂载 CORS 中间件、路由和初始化核心组件。
启动时初始化 ConfigManager, PluginRegistry, CacheManager, KeyStore。

遵循 TECH_SPEC.md:
- 依赖注入: ConfigManager, CacheManager, KeyStore, PluginRegistry 等在启动时初始化
- 子路径部署: 通过环境变量 AI_GATEWAY_BASE_PATH 传入basePath
"""

import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# 确保核心库可导入
_api_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_core_src = os.path.join(_api_root, "..", "aigateway-core", "src")
if _core_src not in sys.path:
    sys.path.insert(0, _core_src)

from aigateway_core.caching import CacheManager
from aigateway_core.circuit_breaker import CircuitBreakerFactory
from aigateway_core.config import ConfigManager
from aigateway_core.logger import setup_logging
from aigateway_core.metrics import get_metrics_collector
from aigateway_core.plugin_registry import PluginRegistry
from aigateway_core.qdrant_client import QdrantClientManager
from aigateway_core.redis_client import RedisClientManager
from aigateway_core.security import KeyStore

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# 全局异常处理器
# ------------------------------------------------------------------


def _register_exception_handlers(app_instance: "FastAPI") -> None:
    """注册 FastAPI 异常处理器，将 GatewayError 层次映射为 HTTP 响应。

    遵循 API_CONTRACT.md 统一错误格式:
    { "error": { "code": "error_code", "message": "人类可读描述" } }
    """
    from fastapi.responses import JSONResponse
    from fastapi import HTTPException

    from aigateway_core.security import (
        AuthError,
        GatewayError,
        QuotaExceededError,
    )

    @app_instance.exception_handler(GatewayError)
    async def gateway_error_handler(
        request: "Request",  # type: ignore[name-defined]
        exc: GatewayError,
    ) -> JSONResponse:
        """基类异常处理器 — 默认 500。"""
        code = "internal_error"
        status = 500
        msg = str(exc)

        # 子类特化
        if isinstance(exc, AuthError):
            code = "unauthorized"
            status = 401
        elif isinstance(exc, QuotaExceededError):
            code = "quota_exceeded"
            status = 429
            msg = str(exc)

        return JSONResponse(
            status_code=status,
            content={"error": {"code": code, "message": msg}},
        )

    @app_instance.exception_handler(HTTPException)
    async def http_exception_handler(
        request: "Request",  # type: ignore[name-defined]
        exc: HTTPException,
    ) -> JSONResponse:
        """FastAPI HTTPException 处理器 — 保留 detail 中的统一错误格式。"""
        detail = exc.detail
        if isinstance(detail, dict) and "error" in detail:
            body = detail
        else:
            body = {"error": {"code": "internal_error", "message": str(detail) if detail else "Internal error"}}

        return JSONResponse(
            status_code=exc.status_code,
            content=body,
        )
def _create_app() -> "FastAPI":
    """创建 FastAPI 应用实例。"""
    # 从环境变量读取 basePath（子路径部署）
    base_path = os.environ.get("AI_GATEWAY_BASE_PATH", "")

    return FastAPI(
        title="AI Gateway API",
        description="OpenAI 兼容的多模型路由网关",
        version="1.0.0",
        lifespan=None,  # 使用自定义 lifespan
    )


# ------------------------------------------------------------------
# 应用生命周期管理
# ------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: "FastAPI"):
    """应用启动/关闭时的资源管理。

    启动时:
    1. 设置日志
    2. 初始化 ConfigManager
    3. 初始化 Redis / Qdrant 连接
    4. 初始化 KeyStore, CacheManager, PluginRegistry
    5. 初始化 CircuitBreakerFactory
    6. 注册默认插件

    关闭时:
    1. 关闭 Redis 和 Qdrant 连接
    """
    # 初始化日志
    log_level = os.environ.get("AI_GATEWAY_LOG_LEVEL", "info").upper()
    setup_logging(log_level=log_level)
    logger.info("AI Gateway API 启动中...")

    # 初始化 ConfigManager
    config_path = os.environ.get("AI_GATEWAY_CONFIG_PATH", "./config.yaml")
    config_manager = ConfigManager(config_path=config_path)
    logger.info("ConfigManager 初始化完成: %s", config_manager.config_path)

    # 初始化 Redis 连接
    redis_url = os.environ.get("AI_GATEWAY_REDIS_URL", "redis://localhost:6379/0")
    redis_mgr = RedisClientManager()
    try:
        await redis_mgr.connect(url=redis_url)
        logger.info("Redis 连接成功: %s", redis_url)
    except Exception as exc:
        logger.warning("Redis 连接失败，部分功能不可用: %s", exc)
        redis_mgr = None  # type: ignore[assignment]

    # 初始化 Qdrant 连接
    qdrant_url = os.environ.get("AI_GATEWAY_QDRANT_URL", "http://localhost:6333")
    qdrant_mgr = QdrantClientManager()
    try:
        await qdrant_mgr.connect(url=qdrant_url)
        logger.info("Qdrant 连接成功: %s", qdrant_url)
    except Exception as exc:
        logger.warning("Qdrant 连接失败，语义缓存功能不可用: %s", exc)
        qdrant_mgr = None  # type: ignore[assignment]

    # 初始化 KeyStore
    key_store: Optional[KeyStore] = None
    if redis_mgr is not None:
        key_store = KeyStore(redis=redis_mgr)
        logger.info("KeyStore 初始化完成")

    # 初始化 CacheManager
    cache_config = config_manager.get("plugins", [])
    prompt_cache_cfg: Dict[str, Any] = {}
    for plugin in cache_config:
        if isinstance(plugin, dict) and plugin.get("name") == "prompt_cache":
            prompt_cache_cfg = plugin.get("config", {})
            break

    cache_manager = CacheManager(
        l1_maxsize=prompt_cache_cfg.get("l1_maxsize", 1000),
        l2_default_ttl=prompt_cache_cfg.get("ttl", 3600),
        l3_default_ttl=prompt_cache_cfg.get("ttl", 86400) if prompt_cache_cfg else 86400,
    )
    if redis_mgr is not None:
        cache_manager.set_redis_client(redis_mgr)
    if qdrant_mgr is not None:
        cache_manager.set_qdrant_client(qdrant_mgr)
    logger.info("CacheManager 初始化完成")

    # 初始化 PluginRegistry
    plugin_registry = PluginRegistry()
    _register_default_plugins(plugin_registry, config_manager)
    logger.info("PluginRegistry 初始化完成: %d 个插件已注册", len(plugin_registry.get_all()))

    # 初始化 CircuitBreakerFactory
    cb_factory = CircuitBreakerFactory()
    logger.info("CircuitBreakerFactory 初始化完成")

    # 持久化到 app.state（唯一数据源）
    import time
    app.state._start_time = int(time.time())

    # 初始化 LiteLLM Bridge
    litellm_bridge = None
    try:
        from aigateway_core.litellm_bridge import LiteLLMBridge
        lb = LiteLLMBridge(config_manager.snapshot())
        providers_cfg = config_manager.get("providers", {})
        if providers_cfg:
            lb.initialize(providers_cfg)
        litellm_bridge = lb
        logger.info("LiteLLM Bridge 初始化完成")
    except Exception as exc:
        logger.warning("LiteLLM Bridge 初始化失败（部分功能不可用）: %s", exc)

    # 挂载到 app.state，供 FastAPI 中间件/依赖注入使用
    app.state.key_store = key_store
    app.state.config_manager = config_manager
    app.state.cache_manager = cache_manager
    app.state.plugin_registry = plugin_registry
    app.state.circuit_breaker_factory = cb_factory

    # 设置 multiprocess 数据目录（多 worker 指标聚合）
    from aigateway_core.metrics import set_multiproc_dir
    set_multiproc_dir("/tmp/prometheus")

    app.state.metrics_collector = get_metrics_collector()
    app.state.litellm_bridge = litellm_bridge
    app.state.redis_manager = redis_mgr
    app.state.qdrant_manager = qdrant_mgr

    # 注册异常处理器
    _register_exception_handlers(app)

    # 挂载路由
    _mount_routes(app)

    logger.info("AI Gateway API 启动完成")

    yield  # 应用运行期间

    # 关闭资源
    logger.info("AI Gateway API 关闭中...")
    if redis_mgr is not None:
        try:
            await redis_mgr.disconnect()
            logger.info("Redis 连接已关闭")
        except Exception as exc:
            logger.warning("关闭 Redis 连接时出错: %s", exc)
    if qdrant_mgr is not None:
        try:
            await qdrant_mgr.disconnect()
            logger.info("Qdrant 连接已关闭")
        except Exception as exc:
            logger.warning("关闭 Qdrant 连接时出错: %s", exc)

    logger.info("AI Gateway API 已关闭")


# ------------------------------------------------------------------
# 路由挂载
# ------------------------------------------------------------------


def _mount_routes(app: "FastAPI") -> None:
    """挂载所有路由到 FastAPI 应用。"""
    from . import admin_routes, openai_compat, routes

    # /v1/* — OpenAI 兼容接口（需要鉴权）
    app.include_router(openai_compat.router, prefix="/v1", tags=["OpenAI 兼容接口"])

    # /admin/* — 管理接口（需要管理员鉴权）
    app.include_router(admin_routes.router, prefix="/admin", tags=["管理接口"])

    # /metrics 和 /health — 基础设施路由（无需鉴权）
    app.include_router(routes.router, tags=["基础设施"])


# ------------------------------------------------------------------
# 默认插件注册
# ------------------------------------------------------------------


def _register_default_plugins(registry: "PluginRegistry", config_manager: "ConfigManager") -> None:
    """注册所有内置插件到注册表。

    使用 pipeline._register_builtin_plugins 统一管理。
    """
    from aigateway_core.pipeline import _register_builtin_plugins

    _register_builtin_plugins(registry, config_manager)


# ------------------------------------------------------------------
# 应用工厂 — 供 uvicorn/gunicorn 使用
# ------------------------------------------------------------------


def create_app() -> "FastAPI":
    """应用工厂函数，供 uWSGI / Gunicorn 调用。

    Returns:
        已配置好 lifespan 的 FastAPI 实例。
    """
    app = _create_app()
    app.router.lifespan_context = lifespan
    return app


# 直接创建全局应用实例
app = _create_app()
app.router.lifespan_context = lifespan
