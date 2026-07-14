"""openai_compat.py 辅助函数单元测试.

覆盖:
- _get_app_state: 组件提取
- _record_request_log: Redis ZSET 写入
- _apply_media_optimization: 多模态/非多模态路径
- _apply_pii_detection: sanitize/reject/pass-through
- _resolve_auto_model: auto/non-auto, resolver/fallback/error
- _apply_prompt_compression: 正常/无插件
- list_models: 有/无 bridge
- create_embeddings: sentence-transformers/OpenAI/错误路径
- _setup_router: router 结构
"""

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-api", "src"))

from aigateway_api import openai_compat


# ==================================================================
# _get_app_state 测试
# ==================================================================


class TestGetAppState:
    def test_returns_expected_keys(self):
        """应返回所有预期键。"""
        mock_state = MagicMock()
        mock_state.cache_manager = MagicMock()
        mock_state.key_store = MagicMock()
        mock_state.litellm_bridge = MagicMock()
        mock_state.metrics_collector = MagicMock()
        mock_state.config_manager = MagicMock()
        mock_state.plugin_registry = MagicMock()
        mock_state.redis_manager = MagicMock()
        mock_state.qdrant_manager = MagicMock()
        mock_state.media_optimization_layer = MagicMock()
        mock_state.media_cache = MagicMock()
        mock_state.pii_detector_plugin = MagicMock()
        mock_state.model_router_resolver = MagicMock()
        mock_state.prompt_compress_plugin = MagicMock()
        mock_state.understanding_engine = MagicMock()
        mock_state.generation_engine = MagicMock()

        with patch('aigateway_api.app_state.get_state', return_value=mock_state):
            result = openai_compat._get_app_state()
            assert result["cache_manager"] is not None
            assert result["litellm_bridge"] is not None
            assert result["understanding_engine"] is not None
            assert result["generation_engine"] is not None

    def test_optional_fields_none(self):
        """可选字段（media_optimization_layer 等）不存在时应为 None。"""
        mock_state = MagicMock()
        mock_state.media_optimization_layer = None
        mock_state.media_cache = None

        with patch('aigateway_api.app_state.get_state', return_value=mock_state):
            result = openai_compat._get_app_state()
            # Should not raise
            assert isinstance(result, dict)


# ==================================================================
# _record_request_log 测试
# ==================================================================


class TestRecordRequestLog:
    @pytest.mark.asyncio
    async def test_no_redis_client_returns_early(self):
        """Redis 客户端为 None 时应静默返回。"""
        with patch.object(openai_compat, '_get_redis_client', return_value=None):
            await openai_compat._record_request_log(
                request=MagicMock(),
                method="POST",
                endpoint="/v1/chat/completions",
                status_code=200,
                duration_ms=150.5,
                model="gpt-4",
                cache_hit=False,
                cache_tier=None,
            )
            # Should not raise

    @pytest.mark.asyncio
    async def test_writes_to_redis_zset(self):
        """正常路径应写入 Redis ZSET。"""
        mock_redis = AsyncMock()
        mock_redis.zadd = AsyncMock()
        mock_redis.zremrangebyrank = AsyncMock()

        with patch.object(openai_compat, '_get_redis_client', return_value=mock_redis):
            fake_request = MagicMock()
            fake_request.state.trace_id = "trace-1"
            fake_request.state.request_id = "req-1"
            fake_request.state.user_id = "user-1"
            fake_request.state.plugin_trace = []

            await openai_compat._record_request_log(
                request=fake_request,
                method="POST",
                endpoint="/v1/chat/completions",
                status_code=200,
                duration_ms=150.5,
                model="gpt-4",
                cache_hit=False,
                cache_tier=None,
            )
            mock_redis.zadd.assert_called_once()
            call_args = mock_redis.zadd.call_args
            assert call_args[0][0] == "aigateway:logs:requests"
            entry = call_args[0][1]
            assert isinstance(entry, dict)
            log_data = json.loads(list(entry.keys())[0])
            assert log_data["method"] == "POST"
            assert log_data["model"] == "gpt-4"
            assert log_data["status"] == 200

    @pytest.mark.asyncio
    async def test_fallback_request_id_when_missing(self):
        """request.state.request_id 不存在时应 fallback 生成。"""
        mock_redis = AsyncMock()
        mock_redis.zadd = AsyncMock()
        mock_redis.zremrangebyrank = AsyncMock()

        with patch.object(openai_compat, '_get_redis_client', return_value=mock_redis):
            # Create a simple state object with real attributes
            class FakeState:
                trace_id = "t-1"
                request_id = None  # triggers fallback
                user_id = ""
                plugin_trace = []

            fake_request = MagicMock()
            fake_request.state = FakeState()

            await openai_compat._record_request_log(
                request=fake_request,
                method="POST",
                endpoint="/v1/chat",
                status_code=200,
                duration_ms=100,
                model="m",
                cache_hit=False,
                cache_tier=None,
            )
            mock_redis.zadd.assert_called_once()


# ==================================================================
# _apply_media_optimization 测试
# ==================================================================


class TestApplyMediaOptimization:
    @pytest.mark.asyncio
    async def test_no_mol_plugin_returns_original(self):
        state = {}
        body = MagicMock()
        body.messages = [{"role": "user", "content": "hello"}]
        result = await openai_compat._apply_media_optimization(body, MagicMock(), state)
        assert result["messages"] == body.messages
        assert result["meta"] == {}

    @pytest.mark.asyncio
    async def test_no_multimodal_returns_original(self):
        """纯文本消息不应触发媒体优化。"""
        mol = MagicMock()
        mol.execute = AsyncMock(return_value=MagicMock(
            request={"messages": [{"role": "user", "content": "hello"}]},
            extra={},
            is_multimodal=False,
            total_token_savings=0,
        ))
        state = {"media_optimization_layer": mol}
        body = MagicMock()
        body.messages = [{"role": "user", "content": "hello"}]
        result = await openai_compat._apply_media_optimization(body, MagicMock(), state)
        assert result["messages"] == body.messages

    @pytest.mark.asyncio
    async def test_multimodal_triggers_processing(self):
        """多模态消息应触发 MOL 处理。"""
        mol = MagicMock()
        ctx = MagicMock()
        ctx.request = {"messages": [{"role": "user", "content": "processed"}]}
        ctx.extra = {"media_optimization": {"detected_types": ["image"], "processors_executed": ["ocr"]}}
        ctx.is_multimodal = True
        ctx.total_token_savings = 500
        mol.execute = AsyncMock(return_value=ctx)
        state = {"media_optimization_layer": mol}
        body = MagicMock()
        body.messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "http://img.jpg"}}]}]
        body.model = "gpt-4o"
        req = MagicMock()
        req.state.trace_id = "t-1"
        result = await openai_compat._apply_media_optimization(body, req, state)
        assert result["messages"][0]["content"] == "processed"
        assert result["meta"]["is_multimodal"] is True
        assert result["meta"]["token_savings"] == 500

    @pytest.mark.asyncio
    async def test_mol_exception_passes_through(self):
        """MOL 异常时应原样透传。"""
        mol = MagicMock()
        mol.execute = AsyncMock(side_effect=Exception("mol error"))
        state = {"media_optimization_layer": mol}
        body = MagicMock()
        body.messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}]
        body.model = "gpt-4o"
        req = MagicMock()
        req.state.trace_id = "t-1"
        result = await openai_compat._apply_media_optimization(body, req, state)
        assert result["messages"] == body.messages


# ==================================================================
# _apply_pii_detection 测试
# ==================================================================


class TestApplyPIIDetection:
    @pytest.mark.asyncio
    async def test_no_pii_plugin_returns_original(self):
        state = {}
        body = MagicMock()
        body.messages = [{"role": "user", "content": "hello"}]
        result = await openai_compat._apply_pii_detection(body, MagicMock(), state)
        assert result["messages"] == body.messages

    @pytest.mark.asyncio
    async def test_pii_sanitized(self):
        """PII 应被脱敏。"""
        ctx = MagicMock()
        ctx.pii_detector = {
            "has_pii": True,
            "detected_categories": ["EMAIL_REDACTED"],
            "strategy": "sanitize",
        }
        ctx.request = {"messages": [{"role": "user", "content": "cleaned"}]}
        pii_plugin = MagicMock()
        pii_plugin.execute = AsyncMock(return_value=ctx)
        state = {"pii_detector_plugin": pii_plugin}
        body = MagicMock()
        body.messages = [{"role": "user", "content": "email: user@example.com"}]
        body.model = "gpt-4"
        req = MagicMock()
        req.state.trace_id = "t-1"
        result = await openai_compat._apply_pii_detection(body, req, state)
        assert result["meta"]["has_pii"] is True
        assert result["meta"]["detected_categories"] == ["EMAIL_REDACTED"]

    @pytest.mark.asyncio
    async def test_pii_reject_returns_error(self):
        """reject 策略应返回错误响应。"""
        ctx = MagicMock()
        ctx.pii_detector = {
            "has_pii": True,
            "detected_categories": ["CREDENTIAL_REDACTED"],
            "strategy": "reject",
            "error": "PII rejected: [CREDENTIAL_REDACTED]",
        }
        ctx.request = {"messages": []}
        pii_plugin = MagicMock()
        pii_plugin.execute = AsyncMock(return_value=ctx)
        state = {"pii_detector_plugin": pii_plugin}
        body = MagicMock()
        body.messages = [{"role": "user", "content": "password: secret"}]
        body.model = "gpt-4"
        req = MagicMock()
        req.state.trace_id = "t-1"
        result = await openai_compat._apply_pii_detection(body, req, state)
        assert "error" in result
        assert result["error"]["code"] == "pii_rejected"
        assert result["status_code"] == 403

    @pytest.mark.asyncio
    async def test_pii_exception_passes_through(self):
        """PII 插件异常时应原样透传。"""
        pii_plugin = MagicMock()
        pii_plugin.execute = AsyncMock(side_effect=Exception("pii error"))
        state = {"pii_detector_plugin": pii_plugin}
        body = MagicMock()
        body.messages = [{"role": "user", "content": "test"}]
        body.model = "gpt-4"
        req = MagicMock()
        req.state.trace_id = "t-1"
        result = await openai_compat._apply_pii_detection(body, req, state)
        assert result["messages"] == body.messages


# ==================================================================
# _resolve_auto_model 测试
# ==================================================================


class TestResolveAutoModel:
    @pytest.mark.asyncio
    async def test_non_auto_model_returns_unchanged(self):
        body = MagicMock()
        body.model = "gpt-4"
        result = await openai_compat._resolve_auto_model(body, {})
        assert result["model"] == "gpt-4"
        assert result["meta"] == {}

    @pytest.mark.asyncio
    async def test_no_resolver_no_bridge_returns_error(self):
        body = MagicMock()
        body.model = "auto"
        body.messages = [{"role": "user", "content": "test"}]
        result = await openai_compat._resolve_auto_model(body, {})
        assert "error" in result
        assert result["status_code"] == 400

    @pytest.mark.asyncio
    async def test_resolver_succeeds(self):
        """resolver 成功时应解析模型。"""
        decision = MagicMock()
        decision.selected_model = "gpt-4-turbo"
        decision.selected_provider = "openai"
        decision.reason = "best_performance"
        decision.estimated_cost = 0.03

        resolver = MagicMock()
        resolver.route = AsyncMock(return_value=decision)
        state = {"model_router_resolver": resolver}
        body = MagicMock()
        body.model = "auto"
        body.messages = [{"role": "user", "content": "Hello, this is a somewhat longer prompt text to test complexity scoring."}]
        result = await openai_compat._resolve_auto_model(body, state)
        assert result["model"] == "gpt-4-turbo"
        assert result["meta"]["selected_model"] == "gpt-4-turbo"

    @pytest.mark.asyncio
    async def test_resolver_fails_falls_back_to_bridge(self):
        """resolver 失败时应回退到 litellm_bridge。"""
        resolver = MagicMock()
        resolver.route = AsyncMock(side_effect=Exception("resolver down"))

        bridge = MagicMock()
        bridge.get_registered_models = MagicMock(return_value=["gpt-4", "gpt-3.5-turbo"])
        state = {"model_router_resolver": resolver, "litellm_bridge": bridge}
        body = MagicMock()
        body.model = "auto"
        body.messages = [{"role": "user", "content": "test"}]
        result = await openai_compat._resolve_auto_model(body, state)
        assert result["model"] == "gpt-4"
        assert result["meta"]["reason"] == "auto_fallback"

    @pytest.mark.asyncio
    async def test_both_fail_returns_error(self):
        """resolver 和 bridge 都失败时应返回错误。"""
        resolver = MagicMock()
        resolver.route = AsyncMock(side_effect=Exception("fail"))
        bridge = MagicMock()
        bridge.get_registered_models = MagicMock(side_effect=Exception("fail"))
        state = {"model_router_resolver": resolver, "litellm_bridge": bridge}
        body = MagicMock()
        body.model = "auto"
        body.messages = [{"role": "user", "content": "test"}]
        result = await openai_compat._resolve_auto_model(body, state)
        assert "error" in result
        assert result["status_code"] == 400


# ==================================================================
# _apply_prompt_compression 测试
# ==================================================================


class TestApplyPromptCompression:
    @pytest.mark.asyncio
    async def test_no_compress_plugin_returns_original(self):
        state = {}
        body = MagicMock()
        body.messages = [{"role": "user", "content": "hello"}]
        result = await openai_compat._apply_prompt_compression(body, MagicMock(), state)
        assert result["messages"] == body.messages

    @pytest.mark.asyncio
    async def test_compress_applied(self):
        ctx = MagicMock()
        ctx.prompt_compress = {
            "original_tokens": 1000,
            "compressed_tokens": 600,
            "compression_ratio": 0.6,
        }
        ctx.request = {"messages": [{"role": "user", "content": "compressed"}]}
        plugin = MagicMock()
        plugin.execute = AsyncMock(return_value=ctx)
        state = {"prompt_compress_plugin": plugin}
        body = MagicMock()
        body.messages = [{"role": "user", "content": "hello world test"}]
        body.model = "gpt-4"
        req = MagicMock()
        req.state.trace_id = "t-1"
        result = await openai_compat._apply_prompt_compression(body, req, state)
        assert result["meta"]["compression_ratio"] == 0.6
        assert result["meta"]["original_tokens"] == 1000

    @pytest.mark.asyncio
    async def test_compress_exception_passes_through(self):
        plugin = MagicMock()
        plugin.execute = AsyncMock(side_effect=Exception("compress error"))
        state = {"prompt_compress_plugin": plugin}
        body = MagicMock()
        body.messages = [{"role": "user", "content": "test"}]
        body.model = "gpt-4"
        req = MagicMock()
        req.state.trace_id = "t-1"
        result = await openai_compat._apply_prompt_compression(body, req, state)
        assert result["messages"] == body.messages


# ==================================================================
# list_models 测试
# ==================================================================


class TestListModels:
    @pytest.mark.asyncio
    async def test_no_bridge_returns_empty_list(self):
        state = {"litellm_bridge": None}
        with patch.object(openai_compat, '_get_app_state', return_value=state):
            req = MagicMock()
            result = await openai_compat.list_models(req)
            assert result.status_code == 200
            content = result.body
            data = json.loads(content)
            assert data["data"]["data"] == []

    @pytest.mark.asyncio
    async def test_bridge_returns_models(self):
        bridge = MagicMock()
        bridge.list_models = AsyncMock(return_value=[{"id": "gpt-4"}, {"id": "gpt-3.5"}])
        state = {"litellm_bridge": bridge}
        with patch.object(openai_compat, '_get_app_state', return_value=state):
            req = MagicMock()
            result = await openai_compat.list_models(req)
            assert result.status_code == 200
            content = result.body
            data = json.loads(content)
            assert len(data["data"]) == 2

    @pytest.mark.asyncio
    async def test_bridge_error_returns_500(self):
        bridge = MagicMock()
        bridge.list_models = AsyncMock(side_effect=Exception("bridge down"))
        state = {"litellm_bridge": bridge}
        with patch.object(openai_compat, '_get_app_state', return_value=state):
            req = MagicMock()
            result = await openai_compat.list_models(req)
            assert result.status_code == 500


# ==================================================================
# create_embeddings 测试
# ==================================================================


class TestCreateEmbeddings:
    @pytest.fixture(autouse=True)
    def _isolate_st_cache(self):
        """Isolate _st_model_cache from leaking between tests."""
        import aigateway_api.openai_compat as oc_module
        self._saved_cache = None
        if hasattr(oc_module, '_st_model_cache'):
            self._saved_cache = oc_module._st_model_cache
            delattr(oc_module, '_st_model_cache')
        yield
        if self._saved_cache is not None:
            oc_module._st_model_cache = self._saved_cache
        elif hasattr(oc_module, '_st_model_cache'):
            delattr(oc_module, '_st_model_cache')
    @pytest.mark.asyncio
    async def test_empty_input_returns_400(self):
        body = MagicMock()
        body.input = ""
        body.model = None
        body.user = None
        req = MagicMock()
        result = await openai_compat.create_embeddings(body, req)
        assert result.status_code == 400

    @pytest.mark.asyncio
    async def test_empty_list_input_returns_400(self):
        body = MagicMock()
        body.input = []
        body.model = None
        body.user = None
        req = MagicMock()
        result = await openai_compat.create_embeddings(body, req)
        assert result.status_code == 400

    @pytest.mark.asyncio
    async def test_whitespace_only_input_returns_400(self):
        body = MagicMock()
        body.input = "   "
        body.model = None
        body.user = None
        req = MagicMock()
        result = await openai_compat.create_embeddings(body, req)
        assert result.status_code == 400

    @pytest.mark.asyncio
    async def test_invalid_input_type_returns_400(self):
        body = MagicMock()
        body.input = 12345
        body.model = None
        body.user = None
        req = MagicMock()
        result = await openai_compat.create_embeddings(body, req)
        assert result.status_code == 400

    @pytest.mark.asyncio
    async def test_sentence_transformers_backend(self):
        """sentence_transformers 后端应返回嵌入向量。"""
        body = MagicMock()
        body.input = "Hello world"
        body.model = "all-MiniLM-L6-v2"
        body.user = None

        mock_emb = MagicMock()
        mock_emb.tolist = MagicMock(return_value=[0.1, 0.2, 0.3])

        st_instance = MagicMock()
        st_instance.encode = MagicMock(return_value=mock_emb)
        st_module_cls = MagicMock()
        st_module_cls.SentenceTransformer = MagicMock(return_value=st_instance)

        with patch.dict(sys.modules, {"sentence_transformers": st_module_cls}):
            req = MagicMock()

            class FakeCM:
                def get(self, key, default=None):
                    return None
            fs = type('FakeState', (), {
                'config_manager': FakeCM(),
                'cache_manager': None,
                'key_store': None,
                'litellm_bridge': None,
                'metrics_collector': None,
                'plugin_registry': None,
                'redis_manager': None,
                'qdrant_manager': None,
            })()

            with patch('aigateway_api.app_state.get_state', return_value=fs):
                import aigateway_api.openai_compat as oc_module
                # Always clean and reset the cache
                if hasattr(oc_module, '_st_model_cache'):
                    delattr(oc_module, '_st_model_cache')
                oc_module._st_model_cache = {"all-MiniLM-L6-v2": st_instance}
                had_openai_compat = hasattr(oc_module, 'openai_compat')
                if not had_openai_compat:
                    oc_module.openai_compat = oc_module
                try:
                    result = await openai_compat.create_embeddings(body, req)
                    assert result.status_code == 200
                    content = json.loads(result.body)
                    assert content["data"]["object"] == "list"
                finally:
                    if not had_openai_compat:
                        delattr(oc_module, 'openai_compat')
                    if hasattr(oc_module, '_st_model_cache'):
                        delattr(oc_module, '_st_model_cache')

    @pytest.mark.asyncio
    async def test_unknown_backend_returns_400(self):
        body = MagicMock()
        body.input = "test"
        body.model = "m"
        body.user = None
        state = {"config_manager": MagicMock()}
        state["config_manager"].get = MagicMock(return_value={"backend": "unknown_backend"})
        with patch.object(openai_compat, '_get_app_state', return_value=state):
            req = MagicMock()
            result = await openai_compat.create_embeddings(body, req)
            assert result.status_code == 400


# ==================================================================
# _setup_router 测试
# ==================================================================


class TestSetupRouter:
    def test_router_has_chat_completions_route(self):
        router = openai_compat._setup_router()
        paths = {route.path: route.methods for route in router.routes}
        assert "/chat/completions" in paths
        assert {"POST"} == paths["/chat/completions"]

    def test_router_has_models_route(self):
        router = openai_compat._setup_router()
        paths = {route.path: route.methods for route in router.routes}
        assert "/models" in paths
        assert {"GET"} == paths["/models"]

    def test_router_has_embeddings_route(self):
        router = openai_compat._setup_router()
        paths = {route.path: route.methods for route in router.routes}
        assert "/embeddings" in paths
        assert {"POST"} == paths["/embeddings"]

    def test_router_has_three_routes(self):
        router = openai_compat._setup_router()
        assert len(router.routes) == 3
