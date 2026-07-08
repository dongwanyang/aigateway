"""
PipelineEngine / Plugin — compatibility shim.

The engine and plugin protocol now live in
``aigateway_core.dispatch.pipeline_engine``. This module re-exports them and
keeps the classic built-in plugin implementations (PII/cache/semantic/compress)
and ``_register_builtin_plugins`` in their original home for backward
compatibility.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from aigateway_core.dispatch.pipeline_engine import PipelineEngine, Plugin  # noqa: F401
from aigateway_core.dispatch.context import PipelineContext
from aigateway_core.prefix.cache.cache_manager import CacheManager
from aigateway_core.prefix.pii.detector import PIIDetector
from aigateway_core.shared.plugin_registry import PluginRegistry

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# 内置插件实现
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
        """执行 PII 检测。

        Args:
            ctx: 管线上下文。

        Returns:
            更新后的上下文。
        """
        messages = ctx.request.get("messages", [])
        if not messages:
            return ctx

        # 拼接所有消息内容为待扫描文本
        texts: list[str] = []
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                # OpenAI 多模态消息：只扫描 text 类型部分
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        texts.append(block.get("text", ""))

        if not texts:
            return ctx

        full_text = "\n".join(texts)

        try:
            sanitized = self.detector.process(full_text)
        except ValueError as exc:
            # reject 策略：检测到 PII 时抛出异常
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

        # Update ctx.request["messages"] with sanitized text so callers
        # reading back from context get the modified messages
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


class PromptCachePlugin:
    """Prompt 缓存插件 — 在管线中实现 L1/L2/L3 缓存查找与回填。

    配置参数:
        cache_manager: CacheManager 实例（由外部注入）
    """

    name: str = "prompt_cache"
    enabled: bool = True
    depends_on: list = []

    def __init__(self, cache_manager: Optional[CacheManager] = None) -> None:
        self.cache_manager = cache_manager

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        """执行缓存查找。

        如果缓存命中，设置 ctx.response 并标记 should_stop。
        如果未命中，不修改上下文，让后续插件/LiteLLM 处理。
        """
        cm = self.cache_manager
        if cm is None:
            return ctx

        # 生成缓存键 v2:与 dispatcher.py 保持一致(同 tenant/scope 生成
        # 相同 hash,避免 engine 路径复活时和 dispatcher 计算出不同 key)。
        # 注:dispatcher 走的是内联缓存,这段 engine 路径实际被 _skip_names
        # 跳过,不参与生产流量。保留同步是为了未来复活时不踩雷。
        messages = ctx.request.get("messages", [])
        # 只保留 system + 末尾 3 轮,与 dispatcher._extract_cacheable_context 语义一致
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

        # 多级缓存查询
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

            # 记录缓存命中信息
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
    """语义缓存插件 — 使用 L3 Qdrant 向量相似度查找相似请求。

    作为 PromptCachePlugin 的补充，专门处理语义级别的缓存命中。

    配置参数:
        cache_manager: CacheManager 实例
        embedding_model: sentence-transformers 模型名
    """

    name: str = "semantic_cache"
    enabled: bool = True
    depends_on: list = ["prompt_cache"]  # 在 prompt_cache 之后执行

    def __init__(
        self,
        cache_manager: Optional[CacheManager] = None,
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

        # 仅在 prompt_cache 未命中时执行语义缓存
        if ctx.cache_hit:
            return ctx

        # 提取待嵌入文本
        messages = ctx.request.get("messages", [])
        texts: list[str] = []
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                texts.append(content)

        if not texts:
            return ctx

        # 计算嵌入向量
        vector = await self._compute_embedding("\n".join(texts))
        if vector is None:
            return ctx

        # 查询 L3
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
            # 模块级缓存，避免每请求加载模型
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
        from .integration_configs import PromptCompressConfig

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
        """延迟初始化 LLMLingua-2 压缩器。ImportError 时标记 passthrough。

        运行设备由 PromptCompressConfig.device 控制（默认 "cpu"，可在 config.yaml
        的 plugins[prompt_compress].config.device 中改为 "cuda" 或 "auto"），
        非法值会回落到 "cpu"。
        """
        try:
            from llmlingua import PromptCompressor

            # LLMLingua PromptCompressor 参数名是 device_map；默认值 "cuda" 在 CPU-only
            # 环境下会抛 "Torch not compiled with CUDA enabled"，因此显式透传配置。
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
        """将 messages 列表拼接为单一文本块用于压缩。

        包含所有消息类型：system、assistant（历史）、user、RAG 注入的内容。
        每条消息以 "[role]: content" 格式拼接，用换行分隔。

        Args:
            messages: OpenAI 格式的消息列表。

        Returns:
            拼接后的完整文本。
        """
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
        """将压缩后的文本重建为 messages 格式。

        策略：保留原始消息结构，将压缩后的文本作为单个 user 消息内容，
        保留第一条 system 消息（如有），其余替换为压缩后的内容。

        Args:
            compressed: 压缩后的文本。
            original_messages: 原始消息列表。

        Returns:
            重建后的 messages 列表。
        """
        if not original_messages:
            return []

        rebuilt: list = []

        # 保留 system 消息（如有）
        for msg in original_messages:
            if msg.get("role") == "system":
                rebuilt.append(msg)
                break

        # 压缩后的内容作为 user 消息
        rebuilt.append({"role": "user", "content": compressed})
        return rebuilt

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        """执行 prompt 压缩。

        1. 从 ctx.request["messages"] 提取完整 prompt
        2. 拼接为单一文本块
        3. 调用 LLMLingua-2 压缩（若可用）
        4. 将压缩后文本写回 ctx.request["messages"]
        5. 记录 original_tokens / compressed_tokens / ratio 到 ctx.prompt_compress
        """
        messages = ctx.request.get("messages", [])
        if not messages:
            return ctx

        # Lazy load LLMLingua-2 on first request (avoids blocking startup)
        self._ensure_compressor_loaded()

        # 若 LLMLingua 不可用，passthrough
        if not self._is_available:
            return ctx

        # 构建完整 prompt 文本
        prompt_text = self._build_prompt_text(messages)
        if not prompt_text.strip():
            return ctx

        # 估算原始 token 数（简化：按空格+标点分词近似）
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

            # 如果压缩结果为空或比原文长，透传原文
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

            # 重建 messages
            compressed_messages = self._rebuild_messages(compressed_text, messages)
            ctx.request["messages"] = compressed_messages

            # 记录指标
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
            # 运行时异常：透传原始 prompt
            logger.warning(
                "LLMLingua-2 压缩运行时异常，透传原始 prompt: %s", exc
            )
            ctx.prompt_compress["original_tokens"] = original_tokens
            ctx.prompt_compress["compressed_tokens"] = original_tokens
            ctx.prompt_compress["compression_ratio"] = 1.0

        return ctx


# ------------------------------------------------------------------
# 辅助模块
# ------------------------------------------------------------------


def _register_builtin_plugins(registry: PluginRegistry, config_manager: Any = None) -> None:
    """注册所有内置插件到注册表。

    Args:
        registry: PluginRegistry 实例。
        config_manager: 可选的配置管理器，用于读取插件配置。
    """
    import json

    plugins_config = []
    if config_manager is not None:
        plugins_config = config_manager.get("plugins", []) or []

    # 获取集成配置（用于 PromptCompressPlugin 等）
    prompt_compress_kwargs: Dict[str, Any] = {}
    if config_manager is not None:
        try:
            integration_cfgs = config_manager.integration_configs
            prompt_compress_kwargs = {"config": integration_cfgs.prompt_compress}
        except Exception:
            pass  # 回退到默认配置

    plugin_map = {
        "pii_detector": (PIIDetectorPlugin, {"strategy": "sanitize"}),
        "prompt_cache": (PromptCachePlugin, {}),
        "semantic_cache": (SemanticCachePlugin, {}),
        "prompt_compress": (PromptCompressPlugin, prompt_compress_kwargs),
    }

    # 注册 RAGRetrieverPlugin（可选依赖）
    try:
        from aigateway_core.plugins.rag_retriever_plugin import RAGRetrieverPlugin

        rag_config = None
        if config_manager is not None:
            try:
                integration_cfgs = config_manager.integration_configs
                rag_config = integration_cfgs.rag_retriever
            except Exception:
                pass

        rag_kwargs: Dict[str, Any] = {}
        if rag_config is not None:
            rag_kwargs["config"] = rag_config

        # Check if enabled via plugins config
        rag_enabled = True
        for pcfg in plugins_config:
            if isinstance(pcfg, dict) and pcfg.get("name") == "rag_retriever":
                rag_enabled = pcfg.get("enabled", True)
                break

        if rag_enabled:
            plugin_map["rag_retriever"] = (RAGRetrieverPlugin, rag_kwargs)
    except ImportError:
        logger.debug("RAGRetrieverPlugin 不可用（导入失败）")

    # 注册 ConvCompressorPlugin（可选依赖）
    try:
        from aigateway_core.plugins.conv_compressor_plugin import ConvCompressorPlugin

        conv_config = None
        if config_manager is not None:
            try:
                integration_cfgs = config_manager.integration_configs
                conv_config = integration_cfgs.conv_compressor
            except Exception:
                pass

        conv_kwargs: Dict[str, Any] = {}
        if conv_config is not None:
            conv_kwargs["config"] = conv_config

        # Check if enabled via plugins config
        conv_enabled = True
        for pcfg in plugins_config:
            if isinstance(pcfg, dict) and pcfg.get("name") == "conv_compressor":
                conv_enabled = pcfg.get("enabled", True)
                break

        if conv_enabled:
            plugin_map["conv_compressor"] = (ConvCompressorPlugin, conv_kwargs)
    except ImportError:
        logger.debug("ConvCompressorPlugin 不可用（导入失败）")

    # 注册 Media Optimization Plugin（V2）
    try:
        from aigateway_core.media.plugin import MediaOptimizationPlugin

        mol_config = {}
        if config_manager is not None:
            mol_config = config_manager.get("media_optimization", {}) or {}

        if mol_config.get("enabled", False):
            plugin_map["media_optimizer"] = (MediaOptimizationPlugin, {"config": mol_config})
    except ImportError:
        logger.debug("Media Optimization Plugin 不可用（导入失败）")

    for name, (plugin_cls, default_config) in plugin_map.items():
        # 查找配置
        cfg = None
        for pcfg in plugins_config:
            if isinstance(pcfg, dict) and pcfg.get("name") == name:
                cfg = pcfg
                break

        enabled = True
        priority = 0
        # 使用类级别 depends_on 作为默认值
        depends_on: list[str] = getattr(plugin_cls, "depends_on", [])
        plugin_config: dict = {}

        if cfg:
            enabled = cfg.get("enabled", True)
            priority = cfg.get("priority", 0)
            depends_on = cfg.get("depends_on", depends_on)
            plugin_config = cfg.get("config", {})

        # 如果 default_config 中已有 "config" 键（使用专用配置对象），
        # 不再用 YAML plugin_config 合并覆盖，仅用 default_config
        if "config" in default_config:
            merged_config = default_config
        else:
            merged_config = {**default_config, **plugin_config}

        registry.register(
            name=name,
            plugin_class=plugin_cls,
            enabled=enabled,
            depends_on=depends_on,
            priority=priority,
            config=merged_config,
        )

    # 注册 Generation Optimization Plugins（6 个优化插件）
    try:
        from aigateway_core.generation_optimization.plugins import (
            register_generation_optimization_plugins,
        )

        gen_opt_config = {}
        if config_manager is not None:
            gen_opt_config = config_manager.get("generation_optimization", {}) or {}

        if gen_opt_config.get("enabled", True):
            # 获取 Redis 客户端（若可用）
            redis_client = None
            try:
                from aigateway_core.redis_client import RedisClientManager

                redis_client = RedisClientManager.get_client()
            except Exception:
                logger.debug("Redis client 不可用，Generation Optimization 插件将使用内存后备")

            register_generation_optimization_plugins(
                registry=registry,
                config_manager=config_manager,
                redis_client=redis_client,
            )
        else:
            logger.info("Generation Optimization Layer 已禁用 (generation_optimization.enabled=false)")
    except ImportError as exc:
        logger.debug("Generation Optimization Plugins 不可用（导入失败）: %s", exc)
