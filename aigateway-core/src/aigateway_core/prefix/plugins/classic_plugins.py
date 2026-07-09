"""Classic built-in plugins: PII detection, caching, semantic cache, prompt compression.

These plugins run in the shared prefix stage before pipeline dispatch.
Moved from root ``pipeline.py`` as part of the 总分总 runtime split.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from aigateway_core.dispatch.context import PipelineContext
from aigateway_core.dispatch.pipeline_engine import Plugin  # noqa: F401
from aigateway_core.prefix.pii.detector import PIIDetector

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# PIIDetectorPlugin
# ------------------------------------------------------------------

class PIIDetectorPlugin:
    """PII 检测插件 — 在请求到达 LLM 前扫描并脱敏敏感信息。

    执行流程:
    1. 从 request.messages 中提取文本内容
    2. 使用 PIIDetector 进行三遍检测（exclusion → named-field → standalone）
    3. 将脱敏后的文本写回 context.pii_detector.sanitized_prompt
    4. 记录检测到的 PII 类别到 context.pii_detector.detected_categories

    配置参数:
        strategy: "sanitize" | "reject" | "hash"，默认 "sanitize"
    """

    name: str = "pii_detector"
    enabled: bool = True
    depends_on: list = []

    def __init__(self, strategy: str = "sanitize") -> None:
        self.detector = PIIDetector(strategy=strategy)

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        """执行 PII 检测。"""
        messages = ctx.request.get("messages", [])
        if not messages:
            return ctx

        texts: list[str] = []
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        texts.append(block.get("text", ""))

        if not texts:
            return ctx

        full_text = "\n".join(texts)

        try:
            sanitized = self.detector.process(full_text)
        except ValueError as exc:
            ctx.mark_stopped(reason=f"PII rejected: {exc}")
            ctx.pii_detector = {
                "error": str(exc),
                "strategy": "reject",
            }
            return ctx

        ctx.pii_detector = {
            "detected_categories": self.detector.detected_categories,
            "strategy": self.detector.strategy,
            "sanitized_prompt": sanitized,
            "has_pii": len(self.detector.detected_categories) > 0,
        }
        ctx.detected_categories = list(self.detector.detected_categories)
        ctx.sanitized_prompt = sanitized

        if sanitized != full_text:
            messages = ctx.request.get("messages", [])
            if messages:
                updated = list(messages)
                for i in reversed(range(len(updated))):
                    msg = updated[i]
                    content = msg.get("content", "")
                    if isinstance(content, str) and content.strip():
                        updated[i] = {**msg, "content": sanitized}
                        break
                    elif isinstance(content, list):
                        new_content = []
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                new_content.append({**block, "text": sanitized})
                            else:
                                new_content.append(block)
                        updated[i] = {**msg, "content": new_content}
                        break
                ctx.request["messages"] = updated

        if self.detector.detected_categories:
            logger.info(
                "PII 检测完成: categories=%s, strategy=%s, request_id=%s",
                self.detector.detected_categories,
                self.detector.strategy,
                ctx.request_id,
            )

        return ctx


# ------------------------------------------------------------------
# PromptCachePlugin
# ------------------------------------------------------------------

class PromptCachePlugin:
    """Prompt 缓存插件 — 在管线中实现 L1/L2/L3 缓存查找与回填。

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

        cache_scope = (ctx.extra.get("cache_scope") or "shared") if isinstance(ctx.extra, dict) else "shared"
        cache_key = cm.generate_cache_key(
            normalized_prompt=normalized,
            model=ctx.request.get("model", ""),
            pipeline_kind=ctx.pipeline_kind or "understanding",
            cache_scope=cache_scope,
            user_id=ctx.user_id or "",
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


# ------------------------------------------------------------------
# SemanticCachePlugin
# ------------------------------------------------------------------

class SemanticCachePlugin:
    """语义缓存插件 — 使用 L3 Qdrant 向量相似度查找相似请求。

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


# ------------------------------------------------------------------
# PromptCompressPlugin
# ------------------------------------------------------------------

class PromptCompressPlugin:
    """Prompt 压缩插件 — LLMLingua-2 Token 级压缩。

    使用 LLMLingua-2 对完整 prompt（含 system/history/user/RAG 上下文）
    进行 token 级压缩，降低发送到 LLM 的 token 数量。

    当 llmlingua 包未安装或运行时异常时，自动降级为 passthrough 模式。
    """

    name: str = "prompt_compress"
    enabled: bool = True
    depends_on: list = ["rag_retriever", "conv_compressor"]

    def __init__(
        self,
        config: Optional["PromptCompressConfig"] = None,
        *,
        compression_ratio: float = 0.5,
    ) -> None:
        from aigateway_core.integration_configs import PromptCompressConfig

        if config is not None:
            self._config = config
        else:
            self._config = PromptCompressConfig(compression_ratio=compression_ratio)

        self._compressor: Any = None
        self._is_available: bool = False
        self._initialized: bool = False

    def _ensure_compressor_loaded(self) -> None:
        """延迟初始化 LLMLingua-2 压缩器（首次请求时加载，避免阻塞启动）."""
        if self._initialized:
            return
        self._initialized = True
        self._init_compressor()

    def _init_compressor(self) -> None:
        """延迟初始化 LLMLingua-2 压缩器。ImportError 时标记 passthrough。"""
        try:
            from llmlingua import PromptCompressor

            device_map = (self._config.device or "cpu").strip().lower()
            if device_map not in ("cpu", "cuda", "auto"):
                logger.warning(
                    "PromptCompressConfig.device=%r 不识别，回落到 cpu",
                    self._config.device,
                )
                device_map = "cpu"
            self._compressor = PromptCompressor(
                model_name=self._config.model_name,
                use_llmlingua2=True,
                device_map=device_map,
            )
            self._is_available = True
            logger.info(
                "LLMLingua-2 压缩器已初始化: model=%s, device=%s",
                self._config.model_name,
                device_map,
            )
        except ImportError:
            self._is_available = False
            logger.warning(
                "llmlingua 包未安装，PromptCompressPlugin 将以 passthrough 模式运行。"
                "安装方式: pip install llmlingua"
            )
        except Exception as exc:
            self._is_available = False
            logger.warning(
                "LLMLingua-2 初始化失败，降级为 passthrough: %s", exc
            )

    def _build_prompt_text(self, messages: list) -> str:
        """将 messages 列表拼接为单一文本块用于压缩。"""
        parts: list = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if content:
                parts.append(f"[{role}]: {content}")
        return "\n".join(parts)

    def _rebuild_messages(
        self, compressed: str, original_messages: list
    ) -> list:
        """将压缩后的文本重建为 messages 格式。"""
        if not original_messages:
            return []

        rebuilt: list = []

        for msg in original_messages:
            if msg.get("role") == "system":
                rebuilt.append(msg)
                break

        rebuilt.append({"role": "user", "content": compressed})
        return rebuilt

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        """执行 prompt 压缩。"""
        messages = ctx.request.get("messages", [])
        if not messages:
            return ctx

        self._ensure_compressor_loaded()

        if not self._is_available:
            return ctx

        prompt_text = self._build_prompt_text(messages)
        if not prompt_text.strip():
            return ctx

        original_tokens = len(prompt_text.split())

        logger.debug(
            "Prompt 压缩开始: original_tokens=%d, target_ratio=%.2f, prompt_preview=%r",
            original_tokens,
            self._config.compression_ratio,
            prompt_text[:120],
        )

        try:
            result = self._compressor.compress_prompt(
                prompt_text,
                rate=self._config.compression_ratio,
                target_token=self._config.target_token if self._config.target_token > 0 else -1,
                force_tokens=self._config.force_tokens,
            )
            compressed_text = result["compressed_prompt"]
            compressed_tokens = len(compressed_text.split())

            if not compressed_text.strip() or compressed_tokens >= original_tokens:
                ctx.prompt_compress["original_tokens"] = original_tokens
                ctx.prompt_compress["compressed_tokens"] = original_tokens
                ctx.prompt_compress["compression_ratio"] = 1.0
                logger.debug(
                    "Prompt 压缩跳过（无收益）: original_tokens=%d, compressed_tokens=%d, "
                    "compressed_empty=%s。常见原因：中文按空格切分粒度过粗、prompt 过短、"
                    "或 LLMLingua-2 判定为不可压缩",
                    original_tokens,
                    compressed_tokens,
                    not bool(compressed_text.strip()),
                )
                return ctx

            compressed_messages = self._rebuild_messages(compressed_text, messages)
            ctx.request["messages"] = compressed_messages

            ratio = compressed_tokens / original_tokens if original_tokens > 0 else 1.0
            ctx.prompt_compress["original_tokens"] = original_tokens
            ctx.prompt_compress["compressed_tokens"] = compressed_tokens
            ctx.prompt_compress["compression_ratio"] = ratio

            logger.debug(
                "Prompt 压缩完成: original_tokens=%d, compressed_tokens=%d, ratio=%.3f",
                original_tokens,
                compressed_tokens,
                ratio,
            )

        except Exception as exc:
            logger.warning(
                "LLMLingua-2 压缩运行时异常，透传原始 prompt: %s", exc
            )
            ctx.prompt_compress["original_tokens"] = original_tokens
            ctx.prompt_compress["compressed_tokens"] = original_tokens
            ctx.prompt_compress["compression_ratio"] = 1.0

        return ctx
