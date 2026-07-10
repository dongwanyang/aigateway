"""Prompt cache + semantic cache plugins - shared prefix stage.

L1/L2/L3 cache lookup and semantic (vector) cache lookup that run before
pipeline dispatch. Split out of the former ``prefix.plugins.classic_plugins``
module as part of the 总分总 runtime split.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from aigateway_core.dispatch.context import PipelineContext

logger = logging.getLogger(__name__)


class PromptCachePlugin:
    """Prompt 缓存插件 - 在管线中实现 L1/L2/L3 缓存查找与回填。

    配置参数:
        cache_manager: CacheManager 实例（由外部注入）
    """

    name: str = "prompt_cache"
    enabled: bool = True
    depends_on: list = []

    def __init__(self, cache_manager: Optional["CacheManager"] = None) -> None:
        self.cache_manager = cache_manager

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        """执行缓存查找。"""
        cm = self.cache_manager
        if cm is None:
            return ctx

        messages = ctx.request.get("messages", [])
        system_msgs = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]
        tail_msgs = non_system[-3:] if len(non_system) > 3 else non_system
        cacheable_msgs = system_msgs + tail_msgs
        normalized = json.dumps(cacheable_msgs, sort_keys=True, ensure_ascii=False)

        cache_scope = (ctx.extra.get("cache_scope") or "group") if isinstance(ctx.extra, dict) else "group"
        group_id = (ctx.extra.get("group_id") or "") if isinstance(ctx.extra, dict) else ""
        cache_key = cm.generate_cache_key(
            normalized_prompt=normalized,
            model=ctx.request.get("model", ""),
            pipeline_kind=ctx.pipeline_kind or "understanding",
            cache_scope=cache_scope,
            user_id=ctx.user_id or "",
            group_id=group_id,
            temperature=ctx.request.get("temperature", 1.0),
            max_tokens=ctx.request.get("max_tokens"),
            top_p=ctx.request.get("top_p"),
        )

        ctx.cache_key = cache_key

        cached = await cm.get(
            cache_key,
            value_fn=None,
            user_id=ctx.user_id,
        )

        if cached is not None and cached.get("hit_tier") in ("L1", "L2", "L3"):
            hit_tier = cached["hit_tier"]
            value = cached["value"]
            ctx.response = value
            ctx.cache_hit = True
            ctx.mark_stopped(reason=f"cache_hit={hit_tier}")

            ctx.prompt_cache = {
                "cache_key": cache_key,
                "cache_hit": True,
                "hit_tier": hit_tier,
            }

            logger.info(
                "缓存命中: tier=%s, request_id=%s",
                hit_tier,
                ctx.request_id,
            )

        return ctx


class SemanticCachePlugin:
    """语义缓存插件 - 使用 L3 Qdrant 向量相似度查找相似请求。

    作为 PromptCachePlugin 的补充，专门处理语义级别的缓存命中。

    配置参数:
        cache_manager: CacheManager 实例
        embedding_model: sentence-transformers 模型名
    """

    name: str = "semantic_cache"
    enabled: bool = True
    depends_on: list = ["prompt_cache"]

    def __init__(
        self,
        cache_manager: Optional["CacheManager"] = None,
        embedding_model: str = "Qwen/Qwen3-Embedding-0.6B",
        **kwargs: Any,
    ) -> None:
        self.cache_manager = cache_manager
        self.embedding_model = embedding_model

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        """执行语义缓存查找。"""
        cm = self.cache_manager
        if cm is None or cm._qdrant_client is None:
            return ctx

        if ctx.cache_hit:
            return ctx

        messages = ctx.request.get("messages", [])
        texts: list[str] = []
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                texts.append(content)

        if not texts:
            return ctx

        vector = await self._compute_embedding("\n".join(texts))
        if vector is None:
            return ctx

        result = await cm.l3_query(
            vector=vector,
            threshold=0.95,
            user_id=ctx.user_id,
        )

        if result is not None:
            response_json = result.get("response_json", "")
            if response_json:
                ctx.response = response_json
                ctx.semantic_cache = {
                    "similarity_score": result.get("score", 0.0),
                    "cached_response": response_json,
                    "hit_count": result.get("hit_count", 0),
                }
                ctx.similarity_score = result.get("score", 0.0)
                ctx.cached_response = response_json
                ctx.mark_stopped(reason="semantic_cache_hit")

                logger.info(
                    "语义缓存命中: score=%.4f, request_id=%s",
                    result.get("score", 0),
                    ctx.request_id,
                )

        return ctx

    async def _compute_embedding(self, text: str) -> Optional[List[float]]:
        """使用 sentence-transformers 计算文本嵌入向量。"""
        try:
            from sentence_transformers import SentenceTransformer
            if not hasattr(SemanticCachePlugin, "_model_cache"):
                SemanticCachePlugin._model_cache: Dict[str, Any] = {}
            model = SemanticCachePlugin._model_cache.get(self.embedding_model)
            if model is None:
                model = SentenceTransformer(self.embedding_model)
                SemanticCachePlugin._model_cache[self.embedding_model] = model
            embedding = model.encode(text, normalize_embeddings=True)
            return embedding.tolist()
        except ImportError:
            logger.warning(
                "sentence-transformers 未安装，无法计算语义缓存向量"
            )
            return None
        except Exception as exc:
            logger.error("嵌入计算失败: %s", exc)
            return None
