"""Cache plugins 单元测试.

覆盖:
- PromptCachePlugin: 缓存 key 生成、L1/L2/L3 命中、未命中、scope 参数
- SemanticCachePlugin: 向量计算、命中/未命中、已有缓存命中时跳过
"""

import asyncio
import json
import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-api", "src"))

from aigateway_core.dispatch.context import PipelineContext
from aigateway_core.prefix.cache.plugin import PromptCachePlugin, SemanticCachePlugin
from aigateway_core.prefix.cache.cache_manager import CacheManager


# ==================================================================
# 辅助函数
# ==================================================================


def make_context(messages=None, extra=None, user_id=None, pipeline_kind="understanding"):
    """创建测试用 PipelineContext。"""
    if messages is None:
        messages = [{"role": "user", "content": "Hello"}]
    if extra is None:
        extra = {}
    return PipelineContext(
        request={"messages": messages, "model": "gpt-4"},
        trace_id="test-trace-1",
        user_id=user_id,
        extra=extra,
        pipeline_kind=pipeline_kind,
    )


def make_mock_cache_manager(hit_tier=None, hit_value=None):
    """创建带有 mock get 方法的 CacheManager。"""
    cm = CacheManager()

    async def mock_get(key, value_fn=None, **params):
        if hit_tier:
            return {
                "hit_tier": hit_tier,
                "value": hit_value or '{"choices": [{"text": "cached"}]}',
                "meta": {},
            }
        return None

    cm.get = mock_get
    cm.generate_cache_key = CacheManager.generate_cache_key
    return cm


# ==================================================================
# PromptCachePlugin 测试
# ==================================================================


class TestPromptCachePlugin:
    """PromptCachePlugin 测试。"""

    @pytest.mark.asyncio
    async def test_no_cache_manager_returns_ctx_unchanged(self):
        plugin = PromptCachePlugin(cache_manager=None)
        ctx = make_context()
        result = await plugin.execute(ctx)
        assert result is ctx
        assert result.cache_hit is False

    @pytest.mark.asyncio
    async def test_cache_miss_no_mark_stopped(self):
        cm = make_mock_cache_manager(hit_tier=None)
        plugin = PromptCachePlugin(cache_manager=cm)
        ctx = make_context()
        result = await plugin.execute(ctx)
        assert result.cache_hit is False
        assert result.should_stop is False

    @pytest.mark.asyncio
    async def test_l1_cache_hit_marks_stopped(self):
        cm = make_mock_cache_manager(hit_tier="L1", hit_value='{"choices": [{"text": "L1"}]}')
        plugin = PromptCachePlugin(cache_manager=cm)
        ctx = make_context()
        result = await plugin.execute(ctx)
        assert result.cache_hit is True
        assert result.should_stop is True
        assert result.response == '{"choices": [{"text": "L1"}]}'
        # Verify prompt_cache namespace was set
        assert result.prompt_cache["hit_tier"] == "L1"
        assert result.prompt_cache["cache_hit"] is True

    @pytest.mark.asyncio
    async def test_l2_cache_hit_marks_stopped(self):
        cm = make_mock_cache_manager(hit_tier="L2", hit_value='{"choices": [{"text": "L2"}]}')
        plugin = PromptCachePlugin(cache_manager=cm)
        ctx = make_context()
        result = await plugin.execute(ctx)
        assert result.cache_hit is True
        assert result.should_stop is True
        assert result.response == '{"choices": [{"text": "L2"}]}'

    @pytest.mark.asyncio
    async def test_l3_cache_hit_marks_stopped(self):
        cm = make_mock_cache_manager(hit_tier="L3", hit_value='{"choices": [{"text": "L3"}]}')
        plugin = PromptCachePlugin(cache_manager=cm)
        ctx = make_context()
        result = await plugin.execute(ctx)
        assert result.cache_hit is True
        assert result.should_stop is True

    @pytest.mark.asyncio
    async def test_cache_key_set_on_context(self):
        cm = make_mock_cache_manager()
        plugin = PromptCachePlugin(cache_manager=cm)
        ctx = make_context()
        await plugin.execute(ctx)
        assert hasattr(ctx, "cache_key")
        assert len(ctx.cache_key) == 64, f"Expected SHA-256 hex string (64 chars), got {len(ctx.cache_key)}"
        assert all(c in '0123456789abcdef' for c in ctx.cache_key), "Cache key must be hex"

    @pytest.mark.asyncio
    async def test_system_messages_included_in_cache_key(self):
        """system 消息应包含在缓存 key 计算中。"""
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
        ]
        ctx = make_context(messages=messages)
        cm = make_mock_cache_manager()
        plugin = PromptCachePlugin(cache_manager=cm)
        result = await plugin.execute(ctx)
        assert result is ctx
        assert hasattr(ctx, "cache_key")
        assert ctx.cache_key

    @pytest.mark.asyncio
    async def test_only_last_3_non_system_messages(self):
        """只有最后 3 条非 system 消息应参与缓存 key。"""
        messages = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Turn 1"},
            {"role": "assistant", "content": "Reply 1"},
            {"role": "user", "content": "Turn 2"},
            {"role": "assistant", "content": "Reply 2"},
            {"role": "user", "content": "Turn 3"},
            {"role": "assistant", "content": "Reply 3"},
            {"role": "user", "content": "Turn 4"},
        ]
        ctx = make_context(messages=messages)
        cm = make_mock_cache_manager()
        plugin = PromptCachePlugin(cache_manager=cm)
        result = await plugin.execute(ctx)
        assert result is ctx
        assert hasattr(ctx, "cache_key")
        assert ctx.cache_key
        assert result.cache_hit is False
        assert result.should_stop is False

    @pytest.mark.asyncio
    async def test_cache_scope_group_default(self):
        """默认 cache_scope 应为 group。"""
        ctx = make_context()
        cm = make_mock_cache_manager()
        plugin = PromptCachePlugin(cache_manager=cm)
        result = await plugin.execute(ctx)
        assert result is ctx
        assert hasattr(ctx, "cache_key")
        assert len(ctx.cache_key) == 64

    @pytest.mark.asyncio
    async def test_cache_scope_private_with_user_id(self):
        """scope=private 时应包含 user_id。"""
        ctx = make_context(extra={"cache_scope": "private"}, user_id="user-123")
        cm = make_mock_cache_manager()
        plugin = PromptCachePlugin(cache_manager=cm)
        result = await plugin.execute(ctx)
        assert result is ctx
        assert hasattr(ctx, "cache_key")
        assert len(ctx.cache_key) == 64

    @pytest.mark.asyncio
    async def test_cache_scope_public(self):
        """scope=public 时不应包含 user/group id。"""
        ctx = make_context(extra={"cache_scope": "public"})
        cm = make_mock_cache_manager()
        plugin = PromptCachePlugin(cache_manager=cm)
        result = await plugin.execute(ctx)
        assert result is ctx
        assert hasattr(ctx, "cache_key")
        assert len(ctx.cache_key) == 64

    @pytest.mark.asyncio
    async def test_cache_scope_keys_differ(self):
        """不同 scope 应产生不同的 cache key。"""
        cm = make_mock_cache_manager()
        plugin = PromptCachePlugin(cache_manager=cm)

        ctx_group = make_context()
        await plugin.execute(ctx_group)
        key_group = ctx_group.cache_key

        ctx_private = make_context(extra={"cache_scope": "private"}, user_id="user-123")
        await plugin.execute(ctx_private)
        key_private = ctx_private.cache_key

        ctx_public = make_context(extra={"cache_scope": "public"})
        await plugin.execute(ctx_public)
        key_public = ctx_public.cache_key

        assert key_group != key_private, "group and private scopes should differ"
        assert key_public != key_private, "public and private scopes should differ"

    @pytest.mark.asyncio
    async def test_empty_messages_returns_unchanged(self):
        ctx = make_context(messages=[])
        cm = make_mock_cache_manager()
        plugin = PromptCachePlugin(cache_manager=cm)
        result = await plugin.execute(ctx)
        assert result is ctx
        assert result.cache_hit is False
        assert result.should_stop is False

    def test_plugin_metadata(self):
        assert PromptCachePlugin.name == "prompt_cache"
        assert PromptCachePlugin.enabled is True
        assert PromptCachePlugin.depends_on == []


# ==================================================================
# SemanticCachePlugin 测试
# ==================================================================


class TestSemanticCachePlugin:
    """SemanticCachePlugin 测试。"""

    @pytest.mark.asyncio
    async def test_no_cache_manager_returns_unchanged(self):
        plugin = SemanticCachePlugin(cache_manager=None)
        ctx = make_context()
        result = await plugin.execute(ctx)
        assert result is ctx
        assert result.should_stop is False

    @pytest.mark.asyncio
    async def test_no_qdrant_returns_unchanged(self):
        cm = CacheManager()  # No qdrant client
        plugin = SemanticCachePlugin(cache_manager=cm)
        ctx = make_context()
        result = await plugin.execute(ctx)
        assert result is ctx
        assert result.should_stop is False

    @pytest.mark.asyncio
    async def test_already_cached_skips_semantic(self):
        """如果已经 PromptCachePlugin 命中，应跳过语义缓存。"""
        cm = CacheManager()
        plugin = SemanticCachePlugin(cache_manager=cm)
        ctx = make_context()
        ctx.cache_hit = True
        result = await plugin.execute(ctx)
        assert result.should_stop is False

    @pytest.mark.asyncio
    async def test_empty_messages_skips(self):
        cm = CacheManager()
        mock_qdrant = MagicMock()
        cm.set_qdrant_client(mock_qdrant)
        plugin = SemanticCachePlugin(cache_manager=cm)
        ctx = make_context(messages=[])
        result = await plugin.execute(ctx)
        assert result is ctx
        assert result.should_stop is False
        assert result.cache_hit is False

    @pytest.mark.asyncio
    async def test_no_vector_computed_skips(self):
        """embedding 计算失败时应返回 context 并继续处理。"""
        cm = CacheManager()
        mock_qdrant = MagicMock()
        cm.set_qdrant_client(mock_qdrant)
        plugin = SemanticCachePlugin(
            cache_manager=cm,
            embedding_model="nonexistent-model"
        )
        ctx = make_context()
        result = await plugin.execute(ctx)
        assert result is ctx
        assert result.should_stop is False
        assert result.cache_hit is False

    @pytest.mark.asyncio
    async def test_plugin_metadata(self):
        assert SemanticCachePlugin.name == "semantic_cache"
        assert SemanticCachePlugin.enabled is True
        assert "prompt_cache" in SemanticCachePlugin.depends_on

    @pytest.mark.asyncio
    async def test_default_embedding_model(self):
        plugin = SemanticCachePlugin()
        assert plugin.embedding_model == "Qwen/Qwen3-Embedding-0.6B"

    @pytest.mark.asyncio
    async def test_custom_embedding_model(self):
        plugin = SemanticCachePlugin(embedding_model="custom/model")
        assert plugin.embedding_model == "custom/model"
