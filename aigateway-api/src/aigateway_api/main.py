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

# 加载 .env 文件到进程环境变量(必须在任何配置读取前执行)
# override=False → 不覆盖已存在的环境变量,保证优先级:
#   进程环境变量(docker environment: / shell export) > .env > config.yaml
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # python-dotenv 未安装时静默跳过,回退到纯环境变量/config.yaml
    pass

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# 确保核心库可导入
_api_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_core_src = os.path.join(_api_root, "..", "aigateway-core", "src")
if _core_src not in sys.path:
    sys.path.insert(0, _core_src)

from aigateway_core.prefix.cache.cache_manager import CacheManager
from aigateway_core.config import ConfigManager
from aigateway_core.logger import setup_logging
from aigateway_core.metrics import get_metrics_collector
from aigateway_core.plugin_registry import PluginRegistry
from aigateway_core.qdrant_client import QdrantClientManager
from aigateway_core.redis_client import RedisClientManager
from aigateway_core.shared.auth.key_store import KeyStore

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

    from aigateway_core.exceptions import (
        AuthError,
        GatewayError,
        QuotaExceededError,
    )

    def _get_request_id(request) -> str:
        """获取或生成 request_id。"""
        if hasattr(request, "state") and hasattr(request.state, "request_id"):
            return request.state.request_id
        return uuid.uuid4().hex[:12]

    def _redact_5xx_msg(msg: str) -> str:
        """5xx 错误信息脱敏 —— 移除常见敏感模式（API key、连接字符串、密码、内部路径、卡号）。

        供 GatewayError 处理器和兜底 Exception 处理器共用，避免 pattern 漂移。
        """
        import re as _re
        _safe_msg = msg
        _patterns = [
            r'sk-[a-zA-Z0-9]{20,}',           # API keys
            r'password\s*[:=]\s*\S+',            # password=...
            r'(mongodb|mysql|postgres|redis)://\S+:@',  # connection strings
            r'/home/\S+|/app/\S+|/opt/\S+',      # internal file paths
            r'\b\d{13,16}\b',                    # credit card numbers
        ]
        for _p in _patterns:
            _safe_msg = _re.sub(_p, '[REDACTED]', _safe_msg)
        return _safe_msg

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

        # 5xx 错误：固定回显 redacted detail（脱敏），不再受 debug_mode 控制
        if status >= 500:
            body["error"]["detail"] = f"{type(exc).__name__}: {_redact_5xx_msg(msg)}"

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

    @app_instance.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: "Request",  # type: ignore[name-defined]
        exc: Exception,
    ) -> JSONResponse:
        """兜底处理器 —— 捕获所有未被上面覆盖的非 GatewayError 异常。

        保证任何 5xx 都返回统一错误结构 {error:{code,message,detail}} + X-Request-ID，
        而不是落到 FastAPI 默认的 {"detail":"Internal Server Error"}。服务端记录完整
        traceback，客户端只回显脱敏后的 type+msg。
        """
        request_id = _get_request_id(request)
        redacted_msg = _redact_5xx_msg(str(exc))
        logger.exception(
            "Unhandled exception (request_id=%s, type=%s): %s",
            request_id,
            type(exc).__name__,
            exc,
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "Internal Server Error",
                    "detail": f"{type(exc).__name__}: {redacted_msg}",
                }
            },
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

    # TraceMiddleware —— 必须最后添加（Starlette last-added = outermost），使其成为
    # 最外层中间件，早于 RateLimiter/CORS 运行，保证 trace_id 全链路一致（含 429 短路场景）。
    from aigateway_api.trace_middleware import TraceMiddleware

    app_instance.add_middleware(TraceMiddleware)
    logger.info("TraceMiddleware 已挂载（最外层）")

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
    5. 注册默认插件

    关闭时:
    1. 关闭 Redis 和 Qdrant 连接
    """
    # 初始化 ConfigManager
    config_path = os.environ.get("AI_GATEWAY_CONFIG_PATH", "./config.yaml")
    config_manager = ConfigManager(config_path=config_path)

    # 日志级别决策优先级：环境变量 > config.yaml observability.log_level > "INFO"
    # （debug_mode 不再强制 DEBUG；AI_GATEWAY_ENV=production 在 config.py 里强制 ≥INFO）
    obs_cfg = config_manager.get("observability", {}) or {}
    env_level = os.environ.get("AI_GATEWAY_LOG_LEVEL")
    cfg_level = obs_cfg.get("log_level")
    if env_level:
        log_level = env_level.upper()
    elif cfg_level:
        log_level = str(cfg_level).upper()
    else:
        log_level = "INFO"

    setup_logging(log_level=log_level)
    logger.info("AI Gateway API 启动中... (log_level=%s)", log_level)

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
    from aigateway_core.prefix.cache.cache_manager import L3CleanupScheduler
    cleanup_interval = int(l3_cfg.get("cleanup_interval", 3600)) // 60 if l3_cfg else 60
    l3_scheduler = L3CleanupScheduler(cache_manager, interval_minutes=cleanup_interval)
    await l3_scheduler.start()

    # 初始化 PluginRegistry
    plugin_registry = PluginRegistry()
    _register_default_plugins(plugin_registry, config_manager)
    logger.info("PluginRegistry 初始化完成: %d 个插件已注册", len(plugin_registry.get_all()))

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
        # 注入 auto 解析器:bridge 收到 model=='auto' 时用它按 pipeline_kind 选模型
        # (总分总架构:「选哪个模型」的决策在管道末端,不在入口)
        if model_router_resolver is not None:
            lb.set_auto_resolver(model_router_resolver)
        litellm_bridge = lb
        logger.info("LiteLLM Bridge 初始化完成")
    except Exception as exc:
        logger.warning("LiteLLM Bridge 初始化失败（部分功能不可用）: %s", exc)

    # 生成管道 wiring：AIDirectorStrategy 延迟绑定 litellm_bridge。
    # register_generation_optimization_plugins 在 bridge 建好前就跑了（在
    # _register_builtin_plugins 内），此处从 registry 取 strategy 单例注入 bridge，
    # 让 ai_director 真正调下游 rewrite_model，而不是走 None 透传。
    if litellm_bridge is not None:
        try:
            ai_dir_reg = plugin_registry._registrations.get("ai_director")
            if ai_dir_reg is not None:
                strategy = ai_dir_reg.config.get("strategy")
                if strategy is not None and hasattr(strategy, "_litellm_bridge"):
                    strategy._litellm_bridge = litellm_bridge
                    logger.info("AIDirectorStrategy 已绑定 litellm_bridge（生成管道 wiring）")
        except Exception as exc:
            logger.warning("AIDirectorStrategy 绑定 litellm_bridge 失败: %s", exc)

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

    # 初始化 5 维度 Debug 开关(PR2 2026-07-05)。attach 到 ConfigManager.on_reload
    # 后,后续 config.yaml 变更自动 atomic swap;首次加载在 attach 内完成。
    from aigateway_core.debug_config import init_debug_config_watcher
    app.state.debug_config_watcher = init_debug_config_watcher(config_manager)
    logger.info("DebugConfigWatcher 已初始化并挂 ConfigManager.on_reload")

    app.state.cache_manager = cache_manager
    app.state.plugin_registry = plugin_registry
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

    # 初始化两条管道的 PipelineEngine（总分总架构的「分」）
    # understanding: pii/cache/semantic/model_router/compress/rag/conv
    # generation: ai_director/.../cost_tracker
    from aigateway_core.pipeline import PipelineEngine

    understanding_engine = PipelineEngine(plugin_registry, pipeline_kind="understanding")
    understanding_engine.initialize()
    generation_engine = PipelineEngine(plugin_registry, pipeline_kind="generation")
    generation_engine.initialize()
    app.state.understanding_engine = understanding_engine
    app.state.generation_engine = generation_engine

    # 注册热重载回调：admin 改 config.yaml 后（经 atomic_swap → _notify_reload）
    # 重建受影响的运行时组件。同步 plugins.enabled 到 registry 并重建两个 Engine。
    # 只有 plugins 段真变化时才重建 engine，避免 update_global_config 之类的 no-op reload
    # 无谓地重跑拓扑排序 / 重建策略实例。
    _last_plugins_snapshot: Dict[str, Any] = {"data": None}

    def _plugins_diff(new_config: dict) -> bool:
        """粗粒度 diff:比较 plugins 段的 (name, enabled) 集合。"""
        new_plugins = new_config.get("plugins", []) or []
        try:
            snap = tuple(
                (p.get("name"), bool(p.get("enabled", True)))
                for p in new_plugins if isinstance(p, dict)
            )
        except Exception:
            snap = None
        prev = _last_plugins_snapshot["data"]
        _last_plugins_snapshot["data"] = snap
        # 首次调用（prev is None）当作有变化处理，保证初始化后的第一次 reload 生效
        return prev is None or snap != prev

    def _on_config_reload(new_config: dict) -> None:
        try:
            registry = getattr(app.state, "plugin_registry", None)
            if registry is None:
                return
            # 无 plugins 变化 → 跳过 registry 同步和 engine 重建
            if not _plugins_diff(new_config):
                logger.debug("热重载:plugins 段无变化，跳过 engine 重建")
                return
            plugins_cfg = new_config.get("plugins", []) or []
            for pcfg in plugins_cfg:
                if not isinstance(pcfg, dict):
                    continue
                name = pcfg.get("name")
                if not name:
                    continue
                reg = getattr(registry, "_registrations", {}).get(name)
                if reg is not None and "enabled" in pcfg:
                    reg.enabled = bool(pcfg["enabled"])
            # 重建两个 Engine（重新装载按 enabled 过滤后的插件链）
            from aigateway_core.pipeline import PipelineEngine
            for kind, attr in (("understanding", "understanding_engine"),
                               ("generation", "generation_engine")):
                eng = PipelineEngine(registry, pipeline_kind=kind)
                eng.initialize()
                setattr(app.state, attr, eng)
            logger.info("热重载回调完成：已同步 plugins.enabled 并重建两条管道 Engine")
        except Exception as exc:
            logger.error("热重载回调执行失败: %s", exc)

    config_manager.on_reload(_on_config_reload)

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
    from . import admin_routes, code_rag_routes, openai_compat, routes, template_routes

    # /v1/* — OpenAI 兼容接口（需要鉴权）
    app.include_router(openai_compat.router, prefix="/v1", tags=["OpenAI 兼容接口"])

    # /admin/* — 管理接口（需要管理员鉴权）
    app.include_router(admin_routes.router, prefix="/admin", tags=["管理接口"])
    app.include_router(code_rag_routes.router, prefix="/admin", tags=["Code RAG"])

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
