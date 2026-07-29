"""Executable application-factory and error-contract tests."""

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from aigateway_api import main
from aigateway_core.shared.exceptions import (
    AuthError,
    GatewayError,
    QuotaExceededError,
)
from fastapi import FastAPI, HTTPException, Request


@pytest.mark.asyncio
async def test_registered_exception_handlers_preserve_contract_and_redact_secrets():
    app = FastAPI()
    main._register_exception_handlers(app)

    @app.get("/gateway")
    async def gateway_error():
        raise GatewayError("failed at /home/service with sk-" + "a" * 24)

    @app.get("/auth")
    async def auth_error():
        raise AuthError("bad credentials")

    @app.get("/quota")
    async def quota_error():
        raise QuotaExceededError("daily exhausted", retry_after=30)

    @app.get("/http")
    async def http_error():
        raise HTTPException(
            status_code=409,
            detail={"error": {"code": "conflict", "message": "duplicate"}},
        )

    @app.get("/plain-http")
    async def plain_http_error():
        raise HTTPException(status_code=404, detail="missing")

    @app.get("/unknown")
    async def unknown_error(request: Request):
        request.state.request_id = "fixed-request-id"
        raise RuntimeError("password=secret /app/private")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        gateway = await client.get("/gateway")
        auth = await client.get("/auth")
        quota = await client.get("/quota")
        structured = await client.get("/http")
        plain = await client.get("/plain-http")
        unknown = await client.get("/unknown")

    assert gateway.status_code == 500
    assert gateway.json()["error"]["code"] == "internal_error"
    assert "[REDACTED]" in gateway.json()["error"]["detail"]
    assert "/home/service" not in gateway.text
    assert "sk-" + "a" * 24 not in gateway.text
    assert len(gateway.headers["x-request-id"]) == 12
    assert auth.status_code == 401
    assert auth.json()["error"] == {"code": "unauthorized", "message": "bad credentials"}
    assert quota.status_code == 429
    assert quota.json()["error"] == {"code": "quota_exceeded", "message": "daily exhausted"}
    assert structured.status_code == 409
    assert structured.json()["error"]["code"] == "conflict"
    assert plain.status_code == 404
    assert plain.json()["error"] == {"code": "internal_error", "message": "missing"}
    assert unknown.status_code == 500
    assert unknown.headers["x-request-id"] == "fixed-request-id"
    assert unknown.json()["error"]["message"] == "Internal Server Error"
    assert "password=secret" not in unknown.text
    assert "/app/private" not in unknown.text


def test_cors_configuration_obeys_config_environment_and_defaults(monkeypatch):
    configured_app = MagicMock()
    configured_manager = MagicMock()
    configured_manager.get.return_value = {
        "cors_origins": ["https://control.example"],
    }
    main._configure_cors(configured_app, configured_manager)
    assert configured_app.add_middleware.call_args.kwargs["allow_origins"] == [
        "https://control.example"
    ]

    environment_app = MagicMock()
    monkeypatch.setenv(
        "AI_GATEWAY_CORS_ORIGINS",
        "https://one.example, https://two.example, ",
    )
    main._configure_cors(environment_app, None)
    assert environment_app.add_middleware.call_args.kwargs["allow_origins"] == [
        "https://one.example",
        "https://two.example",
    ]

    default_app = MagicMock()
    monkeypatch.delenv("AI_GATEWAY_CORS_ORIGINS")
    main._configure_cors(default_app, None)
    assert default_app.add_middleware.call_args.kwargs["allow_origins"] == [
        "http://localhost:3000",
        "http://localhost:5173",
    ]


def test_route_mounting_exposes_each_public_api_family():
    app = FastAPI()
    main._mount_routes(app)
    # FastAPI 0.116+ stores included routers lazily; OpenAPI is the public,
    # flattened route view used by clients.
    paths = set(app.openapi()["paths"])

    assert "/health" in paths
    assert "/metrics" in paths
    assert "/auth/session" in paths
    assert "/v1/chat/completions" in paths
    assert "/v1/videos/{video_id}" in paths
    assert "/admin/api-keys" in paths
    assert "/admin/rag/code/repositories" in paths


def test_default_plugin_registration_delegates_to_core_registry():
    registry = MagicMock()
    config = MagicMock()
    with patch(
        "aigateway_core.prefix.registration._register_builtin_plugins"
    ) as register:
        main._register_default_plugins(registry, config)
    register.assert_called_once_with(registry, config)


@pytest.mark.asyncio
async def test_lifespan_wires_runtime_services_reload_and_shutdown(monkeypatch):
    config_values = {
        "observability": {"log_level": "warning"},
        "infrastructure": {
            "redis": {
                "url": "redis://configured/0",
                "connect_timeout": 2,
                "socket_timeout": 3,
                "health_check_interval": 4,
            },
            "qdrant": {
                "url": "http://qdrant.test",
                "connect_timeout": 2.5,
                "read_timeout": 3.5,
                "write_timeout": 4.5,
            },
        },
        "auth": {
            "api_keys": [{"key": "gw-seed", "group": "Team"}],
            "groups": [
                {"name": "Default"},
                {"name": "Team", "daily_tokens": 1000, "quotas": {"ignored": True}},
            ],
        },
        "plugins": [
            {"name": "prompt_cache", "enabled": True, "config": {"l1_maxsize": 12, "ttl": 34}},
            {"name": "pii_detector", "enabled": True, "config": {"strategy": "block"}},
            {"name": "prompt_compress", "enabled": True, "config": {"compression_ratio": 0.4}},
        ],
        "cache": {
            "l1": {"max_entries": 20, "max_value_bytes": 100},
            "l2": {
                "default_ttl": 40,
                "max_value_bytes": 200,
                "bm25": {"enabled": True},
            },
            "l3": {
                "default_ttl": 60,
                "min_token_count": 5,
                "cleanup_interval": 120,
            },
        },
        "plugin_runtime": {
            "default_timeout_seconds": 7,
            "default_failure_policy": "stop",
            "plugins": {
                "pii_detector": {
                    "timeout_seconds": 2,
                    "failure_policy": "continue",
                },
            },
        },
        "media_optimization": {"enabled": False},
        "generation_optimization": {
            "model_router": {
                "enabled": True,
                "default_model": "model-default",
                "model_capabilities": {"model-default": 80},
            },
            "prompt_templates": {"default_page_size": 10},
        },
        "providers": {},
        "embedding.device": "cpu",
        "intent_classifier": {"model": "classifier-model"},
        "model_selector": {"strategy": "health_first"},
    }
    config = MagicMock()
    config.config_path = "/tmp/config.yaml"
    config.get.side_effect = lambda key, default=None: config_values.get(key, default)
    config.snapshot.return_value = config_values
    reload_callbacks = []
    config.on_reload.side_effect = reload_callbacks.append

    redis = MagicMock()
    redis.redis = MagicMock()
    redis.connect = AsyncMock()
    redis.disconnect = AsyncMock()
    qdrant = MagicMock()
    qdrant.connect = AsyncMock()
    qdrant.disconnect = AsyncMock()

    sqlite = MagicMock()
    sqlite.db_path = "/tmp/auth.db"
    sqlite.seed_from_config = AsyncMock(return_value=1)
    sqlite.ensure_default_group = AsyncMock()
    sqlite.create_group = AsyncMock()
    sqlite.migrate_groups = AsyncMock()
    sqlite.prune_ledger = AsyncMock()

    cache = MagicMock()
    scheduler = MagicMock()
    scheduler.start = AsyncMock()
    scheduler.stop = AsyncMock()

    ai_strategy = SimpleNamespace(_litellm_bridge=None, _model_selector=None)
    draft_shutdown = AsyncMock()
    draft_strategy = SimpleNamespace(
        _litellm_bridge=None,
        _task_tracker=None,
        _store_dir="/tmp/drafts",
        _config=SimpleNamespace(retention_period_hours=12),
        shutdown=draft_shutdown,
    )
    registrations = {
        "ai_director": SimpleNamespace(
            config={"strategy": ai_strategy}, enabled=True
        ),
        "draft_generator": SimpleNamespace(
            config={"strategy": draft_strategy}, enabled=True
        ),
        "prompt_cache": SimpleNamespace(config={}, enabled=True),
    }
    registry = MagicMock()
    registry._registrations = registrations
    registry.get_all.return_value = registrations

    router_resolver = MagicMock()
    router_resolver.get_model_list.return_value = ["model-default"]
    bridge = MagicMock()
    selector = MagicMock()
    classifier = MagicMock()
    template_manager = MagicMock()
    tracker = MagicMock()
    cleaner = MagicMock()
    cleaner.stop = AsyncMock()
    debug_watcher = MagicMock()
    metrics = MagicMock()
    engines = []

    def engine_factory(registry_arg, pipeline_kind):
        engine = MagicMock()
        engine.pipeline_kind = pipeline_kind
        engines.append(engine)
        return engine

    app = FastAPI()
    monkeypatch.setenv("AI_GATEWAY_LOG_LEVEL", "debug")
    monkeypatch.delenv("AI_GATEWAY_REDIS_URL", raising=False)
    monkeypatch.delenv("AI_GATEWAY_QDRANT_URL", raising=False)

    with ExitStack() as stack:
        stack.enter_context(patch.object(main, "ConfigManager", return_value=config))
        stack.enter_context(patch.object(main, "RedisClientManager", return_value=redis))
        stack.enter_context(patch.object(main, "QdrantClientManager", return_value=qdrant))
        stack.enter_context(patch.object(main, "SQLiteStore", return_value=sqlite))
        cache_factory = stack.enter_context(
            patch.object(main, "CacheManager", return_value=cache)
        )
        registry_factory = stack.enter_context(
            patch.object(main, "PluginRegistry", return_value=registry)
        )
        setup_logging = stack.enter_context(patch.object(main, "setup_logging"))
        stack.enter_context(
            patch.object(main, "get_metrics_collector", return_value=metrics)
        )
        register_plugins = stack.enter_context(
            patch.object(main, "_register_default_plugins")
        )
        register_handlers = stack.enter_context(
            patch.object(main, "_register_exception_handlers")
        )
        mount_routes = stack.enter_context(patch.object(main, "_mount_routes"))
        ensure_index = stack.enter_context(patch(
            "aigateway_core.prefix.cache.l2_search.ensure_index",
            new=AsyncMock(return_value=True),
        ))
        scheduler_factory = stack.enter_context(patch(
            "aigateway_core.prefix.cache.cache_manager.L3CleanupScheduler",
            return_value=scheduler,
        ))
        pii_factory = stack.enter_context(patch(
            "aigateway_core.prefix.pii.plugin.PIIDetectorPlugin",
            return_value=SimpleNamespace(),
        ))
        router_factory = stack.enter_context(patch(
            "aigateway_core.route.model_resolution.model_router.ModelRouterStrategy",
            return_value=router_resolver,
        ))
        compressor_factory = stack.enter_context(patch(
            "aigateway_core.pipelines.understanding.compression.plugin.PromptCompressPlugin",
            return_value=SimpleNamespace(),
        ))
        set_device = stack.enter_context(
            patch("aigateway_core.prefix.cache.l3_semantic.set_l3_device")
        )
        bridge_factory = stack.enter_context(patch(
            "aigateway_core.route.bridge.litellm_bridge.LiteLLMBridge",
            return_value=bridge,
        ))
        selector_factory = stack.enter_context(patch(
            "aigateway_core.route.model_resolution.model_selector.ModelSelector",
            return_value=selector,
        ))
        classifier_factory = stack.enter_context(patch(
            "aigateway_core.dispatch.intent_classifier.IntentClassifier",
            return_value=classifier,
        ))
        stack.enter_context(patch(
            "aigateway_core.pipelines.generation.token.prompt_template_manager.PromptTemplateManager",
            return_value=template_manager,
        ))
        stack.enter_context(
            patch("aigateway_api.task_tracker.TaskTracker", return_value=tracker)
        )
        cleaner_factory = stack.enter_context(patch(
            "aigateway_core.pipelines.generation.draft.draft_cleaner.DraftSessionCleaner",
            return_value=cleaner,
        ))
        sweep = stack.enter_context(patch(
            "aigateway_api.code_rag_routes.sweep_orphaned_tasks",
            return_value=2,
        ))
        stack.enter_context(patch(
            "aigateway_core.shared.debug_config.init_debug_config_watcher",
            return_value=debug_watcher,
        ))
        stack.enter_context(patch(
            "aigateway_core.dispatch.pipeline_engine.PipelineEngine",
            side_effect=engine_factory,
        ))
        async with main.lifespan(app):
            assert app.state.config_manager is config
            assert app.state.redis_manager is redis
            assert app.state.qdrant_manager is qdrant
            assert app.state.key_store is sqlite
            assert app.state.cache_manager is cache
            assert app.state.plugin_registry is registry
            assert app.state.draft_strategy is draft_strategy
            assert app.state.task_tracker is tracker
            assert app.state.debug_config_watcher is debug_watcher
            assert app.state.intent_classifier is classifier
            assert app.state.model_selector is selector
            assert app.state.understanding_engine.pipeline_kind == "understanding"
            assert app.state.generation_engine.pipeline_kind == "generation"

            assert len(reload_callbacks) == 1
            callback = reload_callbacks[0]
            callback({"plugins": [{"name": "prompt_cache", "enabled": False}]})
            assert registrations["prompt_cache"].enabled is False
            first_reload_engine_count = len(engines)
            callback({"plugins": [{"name": "prompt_cache", "enabled": False}]})
            assert len(engines) == first_reload_engine_count

    setup_logging.assert_called_once_with(log_level="DEBUG")
    redis.connect.assert_awaited_once_with(
        url="redis://configured/0",
        connect_timeout=2,
        socket_timeout=3,
        health_check_interval=4,
    )
    qdrant.connect.assert_awaited_once_with(
        url="http://qdrant.test",
        connect_timeout=2.5,
        read_timeout=3.5,
        write_timeout=4.5,
    )
    sqlite.seed_from_config.assert_awaited_once_with(
        [{"key": "gw-seed", "group": "Team"}]
    )
    sqlite.ensure_default_group.assert_awaited_once()
    assert sqlite.create_group.await_count == 2
    sqlite.migrate_groups.assert_awaited_once()
    sqlite.prune_ledger.assert_awaited_once_with(keep_days=90)
    cache_factory.assert_called_once_with(
        l1_maxsize=20,
        l2_default_ttl=40,
        l3_default_ttl=60,
        l1_max_value_bytes=100,
        l2_max_value_bytes=200,
        l3_min_token_count=5,
    )
    cache.set_redis_client.assert_called_once_with(redis)
    cache.set_qdrant_client.assert_called_once_with(qdrant)
    ensure_index.assert_awaited_once_with(redis.redis)
    scheduler_factory.assert_called_once_with(cache, interval_minutes=2)
    scheduler.start.assert_awaited_once()
    scheduler.stop.assert_awaited_once()
    registry_factory.assert_called_once_with(
        default_timeout_seconds=7.0,
        default_failure_policy="stop",
        policies={
            "pii_detector": {
                "timeout_seconds": 2,
                "failure_policy": "continue",
            },
        },
    )
    register_plugins.assert_called_once_with(registry, config)
    pii = pii_factory.return_value
    assert pii.timeout_seconds == 2
    assert pii.failure_policy == "continue"
    compressor = compressor_factory.return_value
    assert compressor.timeout_seconds == 7
    assert compressor.failure_policy == "stop"
    router_factory.assert_called_once()
    bridge_factory.assert_called_once_with(config_values)
    bridge.set_auto_resolver.assert_called_once_with(router_resolver)
    selector_factory.assert_called_once_with(
        bridge=bridge,
        config={"strategy": "health_first"},
        default_model="classifier-model",
    )
    classifier_factory.assert_called_once_with(
        bridge=bridge,
        model_selector=selector,
        config={"model": "classifier-model"},
    )
    assert ai_strategy._litellm_bridge is bridge
    assert ai_strategy._model_selector is selector
    assert draft_strategy._litellm_bridge is bridge
    assert draft_strategy._task_tracker is tracker
    assert draft_strategy._redis_client is redis.redis
    cleaner_factory.assert_called_once_with(
        store_dir="/tmp/drafts",
        session_ttl_hours=12,
        strategy=draft_strategy,
    )
    cleaner.start.assert_called_once()
    cleaner.stop.assert_awaited_once()
    draft_shutdown.assert_awaited_once()
    sweep.assert_called_once_with(app.state)
    set_device.assert_called_once_with("cpu")
    register_handlers.assert_called_once_with(app)
    mount_routes.assert_called_once_with(app)
    redis.disconnect.assert_awaited_once()
    qdrant.disconnect.assert_awaited_once()
