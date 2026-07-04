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

    所有错误响应包含 X-Request-ID 响应头。
    """
    from fastapi.responses import JSONResponse
    from fastapi import HTTPException
    import uuid

    from aigateway_core.security import (
        AuthError,
        GatewayError,
        QuotaExceededError,
    )

    def _get_request_id(request) -> str:
        """获取或生成 request_id。"""
        if hasattr(request, "state") and hasattr(request.state, "request_id"):
            return request.state.request_id
        return uuid.uuid4().hex[:12]

    def _is_debug_mode() -> bool:
        """检查当前是否为调试模式。"""
        try:
            config_manager = getattr(app_instance.state, "config_manager", None)
            if config_manager:
                return bool(config_manager.get("debug_mode", False))
        except Exception:
            pass
        return False

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

        request_id = _get_request_id(request)
        body = {"error": {"code": code, "message": msg}}

        # 5xx 错误：调试模式下增加 detail
        if status >= 500 and not _is_debug_mode():
            body["error"]["message"] = "Internal server error"
        elif status >= 500 and _is_debug_mode():
            body["error"]["detail"] = f"{type(exc).__name__}: {msg}"

        return JSONResponse(
            status_code=status,
            content=body,
            headers={"X-Request-ID": request_id},
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

        request_id = _get_request_id(request)
        return JSONResponse(
            status_code=exc.status_code,
            content=body,
            headers={"X-Request-ID": request_id},
        )
def _create_app() -> "FastAPI":
    """创建 FastAPI 应用实例。"""
    # 从环境变量读取 basePath（子路径部署）
    base_path = os.environ.get("AI_GATEWAY_BASE_PATH", "")

    app_instance = FastAPI(
        title="AI Gateway API",
        description="OpenAI 兼容的多模型路由网关",
        version="1.0.0",
        lifespan=None,  # 使用自定义 lifespan
    )

    # CORS 中间件必须在 app 启动前添加
    _configure_cors(app_instance, config_manager=None)

    # 速率限制中间件 (Req 9)
    from .rate_limiter import RateLimiterMiddleware

    # Read rate_limiter config from YAML if available
    _rl_max_requests = 30
    _rl_window_seconds = 60
    try:
        import yaml
        _rl_config_path = os.environ.get("AI_GATEWAY_CONFIG_PATH", "./config.yaml")
        if os.path.isfile(_rl_config_path):
            with open(_rl_config_path, "r", encoding="utf-8") as _f:
                _rl_raw = yaml.safe_load(_f) or {}
            _rl_cfg = _rl_raw.get("rate_limiter", {})
            if isinstance(_rl_cfg, dict):
                _rl_max_requests = int(_rl_cfg.get("max_requests", 30))
                _rl_window_seconds = int(_rl_cfg.get("window_seconds", 60))
    except Exception:
        pass

    app_instance.add_middleware(
        RateLimiterMiddleware,
        max_requests=_rl_max_requests,
        window_seconds=_rl_window_seconds,
        protected_prefixes=("/admin",),
        exempt_paths={"/health", "/metrics"},
    )

    return app_instance


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
    infra_cfg = config_manager.get("infrastructure", {})
    redis_cfg = infra_cfg.get("redis", {}) if isinstance(infra_cfg, dict) else {}
    redis_url = os.environ.get("AI_GATEWAY_REDIS_URL") or redis_cfg.get("url", "redis://localhost:6379/0")
    redis_mgr = RedisClientManager()
    try:
        await redis_mgr.connect(
            url=redis_url,
            connect_timeout=int(redis_cfg.get("connect_timeout", 5)),
            socket_timeout=int(redis_cfg.get("socket_timeout", 10)),
            health_check_interval=int(redis_cfg.get("health_check_interval", 30)),
        )
        logger.info("Redis 连接成功: %s", redis_url)
    except Exception as exc:
        logger.warning("Redis 连接失败，部分功能不可用: %s", exc)
        redis_mgr = None  # type: ignore[assignment]

    # 初始化 Qdrant 连接
    qdrant_cfg = infra_cfg.get("qdrant", {}) if isinstance(infra_cfg, dict) else {}
    qdrant_url = os.environ.get("AI_GATEWAY_QDRANT_URL") or qdrant_cfg.get("url", "http://localhost:6333")
    qdrant_mgr = QdrantClientManager()
    try:
        await qdrant_mgr.connect(
            url=qdrant_url,
            connect_timeout=float(qdrant_cfg.get("connect_timeout", 5.0)),
            read_timeout=float(qdrant_cfg.get("read_timeout", 10.0)),
            write_timeout=float(qdrant_cfg.get("write_timeout", 10.0)),
        )
        logger.info("Qdrant 连接成功: %s", qdrant_url)
    except Exception as exc:
        logger.warning("Qdrant 连接失败，语义缓存功能不可用: %s", exc)
        qdrant_mgr = None  # type: ignore[assignment]

    # 初始化 KeyStore
    key_store: Optional[KeyStore] = None
    if redis_mgr is not None:
        key_store = KeyStore(redis=redis_mgr)
        logger.info("KeyStore 初始化完成")

        # 从 config.yaml 导入 API Key 到 Redis
        auth_config = config_manager.get("auth", {})
        api_keys_config = auth_config.get("api_keys", [])
        if api_keys_config:
            seeded = await key_store.seed_from_config(api_keys_config)
            logger.info("已从 config.yaml 导入 %d 个 API Key 到 Redis", seeded)

    # 初始化 CacheManager
    cache_config = config_manager.get("plugins", [])
    prompt_cache_cfg: Dict[str, Any] = {}
    for plugin in cache_config:
        if isinstance(plugin, dict) and plugin.get("name") == "prompt_cache":
            prompt_cache_cfg = plugin.get("config", {})
            break

    # 读取 cache 配置节
    cache_section = config_manager.get("cache", {})
    l1_cfg = cache_section.get("l1", {}) if isinstance(cache_section, dict) else {}
    l2_cfg = cache_section.get("l2", {}) if isinstance(cache_section, dict) else {}
    l3_cfg = cache_section.get("l3", {}) if isinstance(cache_section, dict) else {}

    cache_manager = CacheManager(
        l1_maxsize=int(l1_cfg.get("max_entries", prompt_cache_cfg.get("l1_maxsize", 1000))),
        l2_default_ttl=int(l2_cfg.get("default_ttl", prompt_cache_cfg.get("ttl", 3600))),
        l3_default_ttl=int(l3_cfg.get("default_ttl", 86400)),
        l1_max_value_bytes=int(l1_cfg.get("max_value_bytes", 102400)),
        l2_max_value_bytes=int(l2_cfg.get("max_value_bytes", 512000)),
        l3_min_token_count=int(l3_cfg.get("min_token_count", 100)),
    )
    if redis_mgr is not None:
        cache_manager.set_redis_client(redis_mgr)
    if qdrant_mgr is not None:
        cache_manager.set_qdrant_client(qdrant_mgr)
    logger.info("CacheManager 初始化完成")

    # 启动 L3 清理调度器
    from aigateway_core.caching import L3CleanupScheduler
    cleanup_interval = int(l3_cfg.get("cleanup_interval", 3600)) // 60 if l3_cfg else 60
    l3_scheduler = L3CleanupScheduler(cache_manager, interval_minutes=cleanup_interval)
    await l3_scheduler.start()

    # 初始化 PluginRegistry
    plugin_registry = PluginRegistry()
    _register_default_plugins(plugin_registry, config_manager)
    logger.info("PluginRegistry 初始化完成: %d 个插件已注册", len(plugin_registry.get_all()))

    # 初始化 CircuitBreakerFactory
    cb_cfg = config_manager.get("circuit_breaker", {})
    cb_factory = CircuitBreakerFactory(
        failure_threshold=int(cb_cfg.get("failure_threshold", 5)) if cb_cfg else 5,
        recovery_timeout=int(cb_cfg.get("recovery_timeout", 60)) if cb_cfg else 60,
        long_open_alert_seconds=int(cb_cfg.get("long_open_alert_seconds", 300)) if cb_cfg else 300,
    )
    logger.info("CircuitBreakerFactory 初始化完成")

    # 初始化 Media Optimization Layer (V2)
    media_optimization_layer = None
    media_cache = None
    try:
        mol_cfg = config_manager.get("media_optimization", {}) or {}
        if mol_cfg.get("enabled", False):
            from aigateway_core.media import MediaCacheManager
            from aigateway_core.media.plugin import MediaOptimizationPlugin

            if redis_mgr is not None:
                media_cache = MediaCacheManager(redis_client=redis_mgr)

            mol_plugin = MediaOptimizationPlugin(config=mol_cfg, media_cache=media_cache)
            media_optimization_layer = mol_plugin
            logger.info(
                "Media Optimization Layer 初始化完成: media_cache=%s",
                "enabled" if media_cache else "disabled (no redis)",
            )
    except Exception as exc:
        logger.warning("Media Optimization Layer 初始化失败: %s", exc)

    # ---- PII Detector Plugin (always enabled) ----
    pii_detector_plugin = None
    try:
        from aigateway_core.pipeline import PIIDetectorPlugin

        pii_cfg = {"strategy": "sanitize"}
        for pcfg in config_manager.get("plugins", []) or []:
            if isinstance(pcfg, dict) and pcfg.get("name") == "pii_detector":
                pii_cfg = pcfg.get("config", pii_cfg)
                break

        pii_detector_plugin = PIIDetectorPlugin(strategy=pii_cfg.get("strategy", "sanitize"))
        logger.info("PIIDetectorPlugin 初始化完成: strategy=%s", pii_cfg.get("strategy", "sanitize"))
    except Exception as exc:
        logger.warning("PIIDetectorPlugin 初始化失败（PII 检测将不可用）: %s", exc)

    # ---- Model Router Resolver (for "auto" model resolution) ----
    model_router_resolver = None
    try:
        from aigateway_core.generation_optimization.config import ModelRouterConfig
        from aigateway_core.generation_optimization.strategies.model_router import ModelRouterStrategy

        gen_opt_cfg = config_manager.get("generation_optimization", {}) or {}
        mr_cfg_data = gen_opt_cfg.get("model_router", {}) or {}
        mr_config = ModelRouterConfig(
            enabled=mr_cfg_data.get("enabled", True),
            default_model=mr_cfg_data.get("default_model", "deepseek-v4-flash"),
            default_capability_score=mr_cfg_data.get("default_capability_score", 50),
            model_capabilities=mr_cfg_data.get("model_capabilities", {}),
            model_modalities=mr_cfg_data.get("model_modalities", {}),
        )
        providers_cfg = config_manager.get("providers", {})
        model_router_resolver = ModelRouterStrategy(
            config=mr_config,
            providers_config=providers_cfg,
        )
        model_list = model_router_resolver.get_model_list()
        logger.info("ModelRouterStrategy 初始化完成: %d models in routing table", len(model_list) if model_list else 0)
    except Exception as exc:
        logger.warning("ModelRouterStrategy 初始化失败（auto 路由将不可用）: %s", exc)

    # ---- Prompt Compress Plugin ----
    prompt_compress_plugin = None
    try:
        from aigateway_core.pipeline import PromptCompressPlugin

        pc_cfg = {}
        for pcfg in config_manager.get("plugins", []) or []:
            if isinstance(pcfg, dict) and pcfg.get("name") == "prompt_compress":
                pc_cfg = pcfg.get("config", {})
                break

        prompt_compress_plugin = PromptCompressPlugin(
            compression_ratio=pc_cfg.get("compression_ratio", 0.5),
        )
        logger.info("PromptCompressPlugin 初始化完成: compression_ratio=%.2f", pc_cfg.get("compression_ratio", 0.5))
    except Exception as exc:
        logger.warning("PromptCompressPlugin 初始化失败（prompt 压缩将不可用）: %s", exc)

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

    # 初始化 PromptTemplateManager
    prompt_template_manager = None
    try:
        from aigateway_core.generation_optimization.config import PromptTemplateConfig
        from aigateway_core.generation_optimization.strategies.prompt_template_manager import PromptTemplateManager

        pt_cfg_raw = config_manager.get("generation_optimization", {})
        pt_cfg_section = pt_cfg_raw.get("prompt_templates", {}) if isinstance(pt_cfg_raw, dict) else {}
        pt_config = PromptTemplateConfig(
            enabled=pt_cfg_section.get("enabled", True),
            default_page_size=int(pt_cfg_section.get("default_page_size", 20)),
            max_page_size=int(pt_cfg_section.get("max_page_size", 100)),
            max_name_length=int(pt_cfg_section.get("max_name_length", 64)),
            max_content_length=int(pt_cfg_section.get("max_content_length", 10000)),
            max_description_length=int(pt_cfg_section.get("max_description_length", 500)),
        )
        prompt_template_manager = PromptTemplateManager(redis_client=redis_mgr, config=pt_config)
        logger.info("PromptTemplateManager 初始化完成")
    except Exception as exc:
        logger.warning("PromptTemplateManager 初始化失败: %s", exc)

    # 挂载到 app.state，供 FastAPI 中间件/依赖注入使用
    app.state.key_store = key_store
    app.state.config_manager = config_manager
    app.state.cache_manager = cache_manager
    app.state.plugin_registry = plugin_registry
    app.state.circuit_breaker_factory = cb_factory
    app.state.l3_cleanup_scheduler = l3_scheduler

    app.state.metrics_collector = get_metrics_collector()
    app.state.litellm_bridge = litellm_bridge
    app.state.redis_manager = redis_mgr
    app.state.qdrant_manager = qdrant_mgr
    app.state.media_optimization_layer = media_optimization_layer
    app.state.media_cache = media_cache
    app.state.prompt_template_manager = prompt_template_manager

    # New plugin instances (inline integration)
    app.state.pii_detector_plugin = pii_detector_plugin
    app.state.model_router_resolver = model_router_resolver
    app.state.prompt_compress_plugin = prompt_compress_plugin

    # 注册异常处理器
    _register_exception_handlers(app)

    # 挂载路由
    _mount_routes(app)

    logger.info("AI Gateway API 启动完成")

    yield  # 应用运行期间

    # 关闭资源
    logger.info("AI Gateway API 关闭中...")
    # 停止 L3 清理调度器
    if hasattr(app.state, 'l3_cleanup_scheduler') and app.state.l3_cleanup_scheduler:
        await app.state.l3_cleanup_scheduler.stop()

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
# CORS 配置
# ------------------------------------------------------------------


def _configure_cors(app: "FastAPI", config_manager: "ConfigManager") -> None:
    """配置 CORS 中间件。

    优先级: server.cors_origins (config.yaml) > AI_GATEWAY_CORS_ORIGINS (env) > 默认值
    """
    # 尝试从 config.yaml 读取
    server_cfg = config_manager.get("server", {}) if config_manager else {}
    cors_origins = server_cfg.get("cors_origins", None)

    if not cors_origins:
        # 尝试从环境变量读取
        cors_env = os.environ.get("AI_GATEWAY_CORS_ORIGINS", "")
        if cors_env:
            cors_origins = [o.strip() for o in cors_env.split(",") if o.strip()]

    if not cors_origins:
        # 默认值
        cors_origins = ["http://localhost:3000", "http://localhost:5173"]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key"],
        allow_credentials=True,
    )
    logger.info("CORS 中间件已配置: origins=%s", cors_origins)


# ------------------------------------------------------------------
# 路由挂载
# ------------------------------------------------------------------


def _mount_routes(app: "FastAPI") -> None:
    """挂载所有路由到 FastAPI 应用。"""
    from . import admin_routes, openai_compat, routes, template_routes

    # /v1/* — OpenAI 兼容接口（需要鉴权）
    app.include_router(openai_compat.router, prefix="/v1", tags=["OpenAI 兼容接口"])

    # /admin/* — 管理接口（需要管理员鉴权）
    app.include_router(admin_routes.router, prefix="/admin", tags=["管理接口"])

    # Generation Optimization — 模板管理等端点
    app.include_router(template_routes.router, tags=["generation-optimization"])

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
