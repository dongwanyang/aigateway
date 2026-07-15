"""
LiteLLM Bridge 单元测试
========================

测试覆盖:
- 模型注册验证（model_not_found 错误）
- 异常变量作用域修复（Python 3.12 兼容）
- resolve_model 映射
- completion / completion_stream 对未注册模型的处理
"""

import asyncio
import sys
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# 确保导入路径正确
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-api", "src"))

from aigateway_core.route.bridge.litellm_bridge import LiteLLMBridge


# ==================================================================
# Helper: 创建已初始化的 Bridge（不依赖真实 litellm）
# ==================================================================


def _create_bridge_with_models(models_config: dict) -> LiteLLMBridge:
    """创建一个带有模型注册的 bridge（mock Router）。"""
    bridge = LiteLLMBridge(config={"providers": models_config})

    # 手动构建 model_list 来填充 _model_alias_map（模拟 initialize）
    model_list = bridge._build_model_list(models_config)

    # Mock router
    bridge.router = MagicMock()
    bridge.router.get_model_list.return_value = model_list

    return bridge


# ==================================================================
# resolve_model Tests
# ==================================================================


class TestResolveModel:
    """resolve_model 映射测试。"""

    def setup_method(self):
        self.bridge = _create_bridge_with_models({
            "openai": {
                "api_key": "sk-test",
                "model_grouper": [{
                    "models": ["gpt-4o", "gpt-4o-mini"],
                    "fallback_models": [],
                }],
            },
            "agnes": {
                "api_key": "sk-agnes-test",
                "base_url": "https://apihub.agnes-ai.com/v1",
                "model_grouper": [{
                    "models": ["agnes-2.0-flash"],
                    "fallback_models": [],
                }],
            },
        })

    def test_bare_model_resolves_to_prefixed(self):
        """裸模型名应解析为带前缀的完整名。"""
        assert self.bridge.resolve_model("gpt-4o") == "openai/gpt-4o"
        assert self.bridge.resolve_model("gpt-4o-mini") == "openai/gpt-4o-mini"

    def test_agnes_resolves_with_openai_prefix(self):
        """base_url 提供商的模型应解析为 openai/ 前缀。"""
        assert self.bridge.resolve_model("agnes-2.0-flash") == "openai/agnes-2.0-flash"

    def test_unknown_model_returns_as_is(self):
        """未注册模型原样返回。"""
        assert self.bridge.resolve_model("deepseek-chat") == "deepseek-chat"

    def test_already_prefixed_returns_as_is(self):
        """已经带前缀的模型原样返回。"""
        assert self.bridge.resolve_model("openai/gpt-4o") == "openai/gpt-4o"


# ==================================================================
# is_model_registered Tests
# ==================================================================


class TestIsModelRegistered:
    """模型注册检查测试。"""

    def setup_method(self):
        self.bridge = _create_bridge_with_models({
            "openai": {
                "api_key": "sk-test",
                "model_grouper": [{
                    "models": ["gpt-4o", "gpt-4o-mini"],
                    "fallback_models": ["gpt-3.5-turbo"],
                }],
            },
            "anthropic": {
                "api_key": "sk-ant-test",
                "model_grouper": [{
                    "models": ["claude-3-5-sonnet"],
                    "fallback_models": [],
                }],
            },
            "agnes": {
                "api_key": "sk-agnes-test",
                "base_url": "https://apihub.agnes-ai.com/v1",
                "model_grouper": [{
                    "models": ["agnes-2.0-flash"],
                    "fallback_models": [],
                }],
            },
        })

    def test_registered_bare_name(self):
        """已注册的裸模型名应返回 True。"""
        assert self.bridge.is_model_registered("gpt-4o") is True
        assert self.bridge.is_model_registered("gpt-4o-mini") is True
        assert self.bridge.is_model_registered("claude-3-5-sonnet") is True
        assert self.bridge.is_model_registered("agnes-2.0-flash") is True

    def test_registered_full_name(self):
        """已注册的完整模型名应返回 True。"""
        assert self.bridge.is_model_registered("openai/gpt-4o") is True
        assert self.bridge.is_model_registered("anthropic/claude-3-5-sonnet") is True
        assert self.bridge.is_model_registered("openai/agnes-2.0-flash") is True

    def test_unregistered_model(self):
        """未注册模型应返回 False。"""
        assert self.bridge.is_model_registered("deepseek-chat") is False
        assert self.bridge.is_model_registered("qwen-turbo") is False
        assert self.bridge.is_model_registered("llama-3-70b") is False

    def test_fallback_model_registered(self):
        """fallback 模型也应被注册。"""
        assert self.bridge.is_model_registered("gpt-3.5-turbo") is True


# ==================================================================
# completion: model_not_found Tests
# ==================================================================


class TestCompletionModelNotFound:
    """completion 对未注册模型的处理测试。"""

    def setup_method(self):
        self.bridge = _create_bridge_with_models({
            "openai": {
                "api_key": "sk-test",
                "model_grouper": [{
                    "models": ["gpt-4o"],
                    "fallback_models": [],
                }],
            },
        })

    @pytest.mark.asyncio
    async def test_unregistered_model_returns_error(self):
        """请求未注册模型 + 无匹配能力池应返回错误。

        新语义: 未注册的显式模型作 hint 被忽略, 转池解析;
        若池也无满足 intent 所需能力的模型 -> no_model_for_intent。
        """
        result = await self.bridge.completion(
            messages=[{"role": "user", "content": "hello"}],
            model="deepseek-chat",
            intent="generation:video",  # 仅注册了 text 模型 gpt-4o, video 池为空
        )

        assert "error" in result
        assert result["error"]["code"] == "no_model_for_intent"

    @pytest.mark.asyncio
    async def test_unregistered_qwen_returns_error(self):
        """请求千问模型未注册 + 无匹配能力池应返回错误。"""
        result = await self.bridge.completion(
            messages=[{"role": "user", "content": "你好"}],
            model="qwen-turbo",
            intent="generation:video",
        )

        assert "error" in result
        assert result["error"]["code"] == "no_model_for_intent"

    @pytest.mark.asyncio
    async def test_registered_model_does_not_return_model_not_found(self):
        """请求已注册模型不应返回 model_not_found 错误。"""
        # Mock the router's acompletion
        mock_response = MagicMock()
        mock_response.dict.return_value = {
            "id": "chatcmpl-test",
            "choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        }
        self.bridge.router.acompletion = AsyncMock(return_value=mock_response)

        result = await self.bridge.completion(
            messages=[{"role": "user", "content": "hello"}],
            model="gpt-4o",
        )

        assert "data" in result
        assert "choices" in result["data"]


# ==================================================================
# completion_stream: model_not_found Tests
# ==================================================================


class TestCompletionStreamModelNotFound:
    """completion_stream 对未注册模型的处理测试。"""

    def setup_method(self):
        self.bridge = _create_bridge_with_models({
            "openai": {
                "api_key": "sk-test",
                "model_grouper": [{
                    "models": ["gpt-4o"],
                    "fallback_models": [],
                }],
            },
        })

    @pytest.mark.asyncio
    async def test_stream_unregistered_model_yields_error(self):
        """流式请求未注册模型 + 无匹配能力池应 yield 错误。"""
        chunks = []
        async for chunk in self.bridge.completion_stream(
            messages=[{"role": "user", "content": "hello"}],
            model="deepseek-chat",
            intent="generation:video",
        ):
            chunks.append(chunk)

        assert len(chunks) == 1
        assert "error" in chunks[0]
        assert chunks[0]["error"]["code"] == "no_model_for_intent"


# ==================================================================
# Python 3.12 Exception Scope Fix Tests
# ==================================================================


class TestExceptionScopeFix:
    """验证 Python 3.12 异常变量作用域修复。"""

    def setup_method(self):
        self.bridge = _create_bridge_with_models({
            "openai": {
                "api_key": "sk-test",
                "model_grouper": [{
                    "models": ["gpt-4o"],
                    "fallback_models": [],
                }],
            },
        })

    @pytest.mark.asyncio
    async def test_stream_all_retries_fail_no_name_error(self):
        """所有重试失败时应返回 upstream_timeout 错误而非 NameError。"""

        # Mock router 的 acompletion: await 后返回 async generator 再抛异常
        async def _failing_stream():
            raise RuntimeError("Connection refused")
            yield  # noqa: unreachable - makes this an async generator

        async def _failing_acompletion(**kwargs):
            return _failing_stream()

        self.bridge.router.acompletion = _failing_acompletion

        chunks = []
        async for chunk in self.bridge.completion_stream(
            messages=[{"role": "user", "content": "hello"}],
            model="gpt-4o",
            max_retries=1,
        ):
            chunks.append(chunk)

        # 应该拿到错误 chunk 而不是 NameError
        assert len(chunks) >= 1
        last_chunk = chunks[-1]
        assert "error" in last_chunk
        assert last_chunk["error"]["code"] == "upstream_timeout"
        assert "Connection refused" in last_chunk["error"]["message"]

    @pytest.mark.asyncio
    async def test_non_stream_all_retries_fail(self):
        """非流式所有重试失败时应正确返回错误。"""
        self.bridge.router.acompletion = AsyncMock(
            side_effect=RuntimeError("Timeout")
        )

        result = await self.bridge.completion(
            messages=[{"role": "user", "content": "hello"}],
            model="gpt-4o",
            max_retries=1,
        )

        assert "error" in result
        assert result["error"]["code"] == "upstream_timeout"
        assert "Timeout" in result["error"]["message"]


# ==================================================================
# get_registered_models Tests
# ==================================================================


class TestGetRegisteredModels:
    """get_registered_models 测试。"""

    def test_returns_all_bare_names(self):
        bridge = _create_bridge_with_models({
            "openai": {
                "api_key": "sk-test",
                "model_grouper": [{
                    "models": ["gpt-4o", "gpt-4o-mini"],
                    "fallback_models": ["gpt-3.5-turbo"],
                }],
            },
            "agnes": {
                "api_key": "sk-agnes",
                "base_url": "https://apihub.agnes-ai.com/v1",
                "model_grouper": [{
                    "models": ["agnes-2.0-flash"],
                    "fallback_models": [],
                }],
            },
        })

        models = bridge.get_registered_models()
        assert "gpt-4o" in models
        assert "gpt-4o-mini" in models
        assert "gpt-3.5-turbo" in models
        assert "agnes-2.0-flash" in models


# ==================================================================
# Per-Model base_url Override Tests
# ==================================================================


class TestPerModelBaseUrl:
    """Per-model base_url override 行为测试。"""

    def test_per_model_base_url_overrides_provider(self):
        """设置了 per-model base_url 的模型应使用自己的 URL。"""
        bridge = _create_bridge_with_models({
            "agnes": {
                "api_key": "sk-test",
                "base_url": "https://provider-default.com/v1",
                "model_grouper": [{
                    "models": [
                        {"name": "text-model"},
                        {"name": "image-model", "base_url": "https://image-endpoint.com/v1"},
                        {"name": "video-model", "base_url": "https://video-endpoint.com/v1"},
                    ],
                    "fallback_models": [],
                }],
            },
        })
        model_list = bridge._build_model_list(bridge.config.get("providers", {}))

        text_entry = next(m for m in model_list if m["model_name"] == "openai/text-model")
        image_entry = next(m for m in model_list if m["model_name"] == "openai/image-model")
        video_entry = next(m for m in model_list if m["model_name"] == "openai/video-model")

        assert text_entry["litellm_params"]["base_url"] == "https://provider-default.com/v1"
        assert image_entry["litellm_params"]["base_url"] == "https://image-endpoint.com/v1"
        assert video_entry["litellm_params"]["base_url"] == "https://video-endpoint.com/v1"

    def test_fallback_uses_provider_base_url_not_per_model(self):
        """Fallback 模型必须始终使用 provider 级别 base_url，不继承主模型的 custom URL。"""
        providers_config = {
            "agnes": {
                "api_key": "sk-test",
                "base_url": "https://provider-default.com/v1",
                "model_grouper": [{
                    "models": [
                        {"name": "primary", "base_url": "https://custom.com/v1"},
                    ],
                    "fallback_models": ["fb-model"],
                }],
            },
        }
        bridge = LiteLLMBridge(config={"providers": providers_config})
        model_list = bridge._build_model_list(providers_config)

        fb_entry = next(m for m in model_list if m["model_name"] == "openai/fb-model")
        assert fb_entry["litellm_params"]["base_url"] == "https://provider-default.com/v1"

    def test_no_per_model_base_url_falls_back_to_provider(self):
        """未设置 per-model base_url 的模型回退到 provider 级别。"""
        bridge = _create_bridge_with_models({
            "agnes": {
                "api_key": "sk-test",
                "base_url": "https://provider-default.com/v1",
                "model_grouper": [{
                    "models": [
                        {"name": "no-override"},
                    ],
                    "fallback_models": [],
                }],
            },
        })
        model_list = bridge._build_model_list(bridge.config.get("providers", {}))
        entry = next(m for m in model_list if m["model_name"] == "openai/no-override")
        assert entry["litellm_params"]["base_url"] == "https://provider-default.com/v1"

    def test_string_format_models_use_provider_base_url(self):
        """字符串格式的旧式 model entry 仍使用 provider 级别 base_url。"""
        bridge = _create_bridge_with_models({
            "openai": {
                "api_key": "sk-test",
                "base_url": "https://openai.com/v1",
                "model_grouper": [{
                    "models": ["gpt-4o"],
                    "fallback_models": [],
                }],
            },
        })
        model_list = bridge._build_model_list(bridge.config.get("providers", {}))
        entry = next(m for m in model_list if m["model_name"] == "openai/gpt-4o")
        assert entry["litellm_params"]["base_url"] == "https://openai.com/v1"

    def test_empty_string_base_url_treated_as_not_set(self):
        """空字符串的 per-model base_url 应等同未设置，回退到 provider 级别。"""
        bridge = _create_bridge_with_models({
            "agnes": {
                "api_key": "sk-test",
                "base_url": "https://provider.com/v1",
                "model_grouper": [{
                    "models": [
                        {"name": "empty-url", "base_url": ""},
                    ],
                    "fallback_models": [],
                }],
            },
        })
        model_list = bridge._build_model_list(bridge.config.get("providers", {}))
        entry = next(m for m in model_list if m["model_name"] == "openai/empty-url")
        assert entry["litellm_params"]["base_url"] == "https://provider.com/v1"

    def test_per_model_base_url_when_provider_has_none(self):
        """Provider 无 base_url 但模型有 per-model base_url 时，模型走 OpenAI 兼容模式。"""
        bridge = _create_bridge_with_models({
            "custom": {
                "api_key": "sk-test",
                # provider 级别无 base_url
                "model_grouper": [{
                    "models": [
                        {"name": "text-only"},
                        {"name": "custom-endpoint", "base_url": "https://custom.com/v1"},
                    ],
                    "fallback_models": [],
                }],
            },
        })
        model_list = bridge._build_model_list(bridge.config.get("providers", {}))

        # 无任何 base_url 的模型走 native 路由前缀
        text_entry = next(m for m in model_list if m["model_name"] == "custom/text-only")
        assert text_entry["litellm_params"]["base_url"] is None

        # 有 per-model base_url 的模型走 openai/ 前缀
        custom_entry = next(m for m in model_list if m["model_name"] == "openai/custom-endpoint")
        assert custom_entry["litellm_params"]["base_url"] == "https://custom.com/v1"


# ==================================================================
# capabilities pool + intent-based resolution Tests
# ==================================================================


class TestCapabilitiesPool:
    """capabilities 多选 + 按意图过滤候选池."""

    def _bridge_with_caps(self):
        models_config = {
            "agnes": {
                "api_key": "k",
                "base_url": "https://apihub.agnes-ai.com/v1",
                "model_grouper": [
                    {
                        "models": [
                            {"name": "agnes-2.0-flash", "capabilities": ["text", "image", "video"]},
                            {"name": "agnes-image-2.1-flash", "capabilities": ["image"]},
                            {"name": "deepseek-v4-flash", "capabilities": ["text"]},
                        ],
                        "fallback_models": [],
                        "pricing": {},
                    }
                ],
            }
        }
        return _create_bridge_with_models(models_config)

    def test_capabilities_recorded(self):
        bridge = self._bridge_with_caps()
        assert "text" in bridge._model_capabilities["agnes-2.0-flash"]
        assert bridge._model_capabilities["agnes-image-2.1-flash"] == ["image"]

    @pytest.mark.asyncio
    async def test_resolve_by_intent_image_pools_image_models(self):
        bridge = self._bridge_with_caps()
        resolved = await bridge._resolve_by_intent(intent="generation:image", model_hint=None)
        assert "error" not in resolved
        assert resolved["model"] in ("agnes-2.0-flash", "agnes-image-2.1-flash")

    @pytest.mark.asyncio
    async def test_resolve_by_intent_hint_in_pool_preferred(self):
        bridge = self._bridge_with_caps()
        resolved = await bridge._resolve_by_intent(
            intent="generation:image", model_hint="agnes-2.0-flash"
        )
        assert resolved["model"] == "agnes-2.0-flash"

    @pytest.mark.asyncio
    async def test_resolve_by_intent_hint_not_in_pool_ignored(self):
        bridge = self._bridge_with_caps()
        # hint 是 text 模型, 但意图是 image -> 忽略 hint, 选 image 池
        resolved = await bridge._resolve_by_intent(
            intent="generation:image", model_hint="deepseek-v4-flash"
        )
        assert resolved["model"] in ("agnes-2.0-flash", "agnes-image-2.1-flash")

    @pytest.mark.asyncio
    async def test_resolve_by_intent_empty_pool_returns_error(self):
        bridge = self._bridge_with_caps()
        # 无 video-only 外的… 实际 agnes-2.0-flash 含 video, 改成移除它
        bridge._model_capabilities = {
            "agnes-2.0-flash": ["text", "image"],
            "agnes-image-2.1-flash": ["image"],
            "deepseek-v4-flash": ["text"],
        }
        resolved = await bridge._resolve_by_intent(intent="generation:video", model_hint=None)
        assert "error" in resolved
        assert resolved["error"]["code"] == "no_model_for_intent"

    @pytest.mark.asyncio
    async def test_transition_alias_generation_to_image(self):
        """Old pipeline_kind='generation' should be mapped to 'generation:image'."""
        bridge = self._bridge_with_caps()
        # Mock _do_image_generation so we don't hit real HTTP
        bridge._do_image_generation = AsyncMock(return_value={
            "choices": [{"message": {"role": "assistant", "content": "url"}}],
            "usage": {},
        })
        # Use an unregistered model so _resolve_by_intent IS called
        result = await bridge.completion(
            messages=[{"role": "user", "content": "draw a cat"}],
            model="unregistered-image-model",
            pipeline_kind="generation",  # old-style param -> maps to generation:image
        )
        assert "data" in result
        # Verify _do_image_generation was called (proves intent routed to image)
        bridge._do_image_generation.assert_called_once()

    @pytest.mark.asyncio
    async def test_explicit_intent_overrides_pipeline_kind(self):
        """Explicit intent=video should override pipeline_kind=image."""
        bridge = self._bridge_with_caps()
        # Mock _do_video_generation since video intent goes there
        bridge._do_video_generation = AsyncMock(return_value={
            "task_id": "vid-1", "status": "queued"
        })

        result = await bridge.completion(
            messages=[{"role": "user", "content": "生成视频"}],
            model="agnes-2.0-flash",
            intent="generation:video",
            pipeline_kind="generation",  # old-style param -> should be ignored
        )
        assert "data" in result
        # Should go through _do_video_generation (video path), NOT _do_image_generation
        bridge._do_video_generation.assert_called_once()
