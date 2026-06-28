"""
PipelineEngine — 异步插件管线引擎
================================

按配置顺序执行插件管线，支持短路（should_stop=True 时跳过后续插件）、
依赖校验和插件级耗时追踪。

根据 API_CONTRACT.md _meta.plugin_trace 定义。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Protocol

import json

from .caching import CacheManager
from .context import PipelineContext
from .plugin_registry import PluginRegistry
from .security import PIIDetector

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# 插件协议
# ------------------------------------------------------------------


class Plugin(Protocol):
    """插件接口协议，所有管线插件必须实现此接口。"""

    name: str
    enabled: bool
    depends_on: List[str]

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        """执行插件逻辑。

        Args:
            ctx: 当前请求的共享上下文。

        Returns:
            修改后的上下文。

        Raises:
            Exception: 插件执行失败时向上抛出，由 Engine 捕获并记录。
        """
        ...


# ------------------------------------------------------------------
# 管线引擎
# ------------------------------------------------------------------


class PipelineEngine:
    """异步插件管线引擎。

    维护已注册插件的有序列表，按拓扑排序后的顺序依次执行。
    支持短路机制：任一插件将 ctx.should_stop 设为 True 后，
    后续插件将被跳过。

    属性:
        registry: 插件注册表实例。
        _ordered_plugins: 排序后的插件列表（在 init 时计算）。
    """

    def __init__(self, registry: PluginRegistry) -> None:
        """
        Args:
            registry: PluginRegistry 实例，包含所有已注册的插件。
        """
        self.registry = registry
        # 缓存排序结果，避免每次请求都重新排序
        self._ordered_plugins: List[Plugin] = []
        self._initialized = False

    def initialize(self) -> None:
        """初始化管线：从注册表中取出已启用的插件并按拓扑排序。

        此方法通常在服务启动时调用一次。
        """
        all_plugins = self.registry.get_all()
        # 仅保留 enabled=True 的插件
        enabled_plugins = [p for p in all_plugins if getattr(p, "enabled", True)]

        # 拓扑排序（考虑 depends_on）
        sorted_plugins = self._topological_sort(enabled_plugins)

        self._ordered_plugins = sorted_plugins
        self._initialized = True

        logger.info(
            "PipelineEngine 已初始化: %d 个插件按序排列",
            len(sorted_plugins),
        )
        for i, plugin in enumerate(sorted_plugins):
            deps = getattr(plugin, "depends_on", [])
            logger.debug("  [%d] %s (依赖: %s)", i, plugin.name, deps)

    async def execute(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """执行完整插件管线。

        流程:
        1. 创建 PipelineContext
        2. 按序执行每个插件
        3. 任一插件短路时提前终止
        4. 返回最终响应

        Args:
            request: 原始 OpenAI 格式请求体。

        Returns:
            包含最终响应的字典，结构见 API_CONTRACT.md。
        """
        if not self._initialized:
            self.initialize()

        # 创建上下文
        ctx = PipelineContext(request=request)
        ctx.should_stream = bool(request.get("stream", False))

        # 记录管线开始时间
        pipeline_start = time.monotonic()

        try:
            # 依次执行每个插件
            for plugin in self._ordered_plugins:
                # 短路检查
                if ctx.should_stop:
                    # 记录跳过的插件
                    skipped_ms = (time.monotonic() - pipeline_start) * 1000
                    plugin_start = time.monotonic()
                    ctx.add_plugin_trace(plugin.name, skipped_ms, "skipped")
                    logger.debug(
                        "插件 %s 被跳过 (should_stop=True, request_id=%s)",
                        plugin.name,
                        ctx.request_id,
                    )
                    continue

                # 执行插件
                plugin_name = plugin.name
                plugin_start = time.monotonic()

                try:
                    ctx = await plugin.execute(ctx)
                except Exception as exc:
                    elapsed_ms = (time.monotonic() - plugin_start) * 1000
                    ctx.add_plugin_trace(plugin_name, elapsed_ms, "failed")
                    logger.error(
                        "插件 %s 执行失败: %s, request_id=%s",
                        plugin_name,
                        exc,
                        ctx.request_id,
                    )
                    # 插件失败时继续执行后续插件（故障隔离）
                    # 若需熔断可在此处设置 ctx.should_stop = True
                    continue

                elapsed_ms = (time.monotonic() - plugin_start) * 1000
                ctx.add_plugin_trace(plugin_name, elapsed_ms, "success")
                logger.debug(
                    "插件 %s 执行完毕: %.2fms, request_id=%s",
                    plugin_name,
                    elapsed_ms,
                    ctx.request_id,
                )

            # 管线执行完成
            total_ms = (time.monotonic() - pipeline_start) * 1000
            logger.info(
                "管线执行完成: request_id=%s, total=%.2fms, stopped=%s",
                ctx.request_id,
                total_ms,
                ctx.should_stop,
            )

            # 构建响应
            response = self._build_response(ctx)
            return response

        except Exception as exc:
            # 管线级异常兜底
            logger.error(
                "管线执行发生未捕获异常: %s, request_id=%s",
                exc,
                getattr(ctx, "request_id", "unknown"),
            )
            return self._build_error_response(str(exc))

    def _topological_sort(self, plugins: List[Plugin]) -> List[Plugin]:
        """对插件列表进行拓扑排序（确保 depends_on 先执行）。

        使用 Kahn 算法的简化版，支持平级插件任意排序。

        Args:
            plugins: 待排序的插件列表。

        Returns:
            排序后的插件列表。
        """
        name_to_plugin: Dict[str, Plugin] = {p.name: p for p in plugins}
        # 入度表
        in_degree: Dict[str, int] = {p.name: 0 for p in plugins}
        # 依赖边
        dependents: Dict[str, List[str]] = {p.name: [] for p in plugins}

        for plugin in plugins:
            deps = getattr(plugin, "depends_on", [])
            for dep in deps:
                if dep in name_to_plugin:
                    in_degree[plugin.name] += 1
                    dependents[dep].append(plugin.name)
                else:
                    # 依赖的插件不存在但已禁用，忽略
                    logger.warning(
                        "插件 %s 依赖 %s 不存在或被禁用，已忽略",
                        plugin.name,
                        dep,
                    )

        # Kahn 算法
        queue: List[str] = []
        for name, degree in in_degree.items():
            if degree == 0:
                queue.append(name)

        ordered_names: List[str] = []
        while queue:
            # 取第一个入度为 0 的节点
            node = queue.pop(0)
            ordered_names.append(node)

            for dependent in dependents[node]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        # 检查环
        if len(ordered_names) != len(plugins):
            missing = [p.name for p in plugins if p.name not in ordered_names]
            logger.error("插件依赖存在循环: %s", missing)
            # 有环时降级：返回未排序的原始列表
            return plugins

        return [name_to_plugin[name] for name in ordered_names]

    def _build_response(self, ctx: PipelineContext) -> Dict[str, Any]:
        """根据上下文构建最终响应。

        若 response 已被缓存插件填充，直接返回；否则
        走默认的空响应占位。

        Args:
            ctx: 管线执行结束后的上下文。

        Returns:
            符合 API_CONTRACT.md 成功响应结构的字典。
        """
        response_data: Dict[str, Any] = {}

        if ctx.response:
            # 缓存命中：response 已是完整 JSON 字符串
            import json
            try:
                parsed = json.loads(ctx.response)
                response_data = parsed.get("data", parsed)
            except (json.JSONDecodeError, AttributeError):
                response_data = {"raw": ctx.response}
        else:
            # 未命中缓存，需要 LiteLLM Bridge 提供实际响应
            # 此处返回占位，实际响应由路由层组装
            response_data = {"status": "needs_completion"}

        return {
            "data": response_data,
            "message": "success",
            "_meta": {
                "cache_hit": bool(ctx.response),
                "cache_tier": "L1" if ctx.response else None,
                "plugin_trace": ctx.get_plugin_trace(),
                "routed_to": None,  # 由 litellm_bridge.completion 填充
            },
        }

    def _build_error_response(self, message: str) -> Dict[str, Any]:
        """构建错误响应。

        Args:
            message: 错误描述。

        Returns:
            符合 API_CONTRACT.md 错误响应结构的字典。
        """
        return {
            "error": {
                "code": "internal_error",
                "message": f"Internal gateway error: {message}",
            }
        }


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

        # 生成缓存键
        messages = ctx.request.get("messages", [])
        normalized = json.dumps(messages, sort_keys=True, ensure_ascii=False)
        cache_key = cm.generate_cache_key(
            normalized_prompt=normalized,
            model=ctx.request.get("model", ""),
            temperature=ctx.request.get("temperature", 1.0),
            max_tokens=ctx.request.get("max_tokens") or 0,
            top_p=ctx.request.get("top_p", 1.0),
            user_id=ctx.user_id or "",
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
        embedding_model: str = "all-MiniLM-L6-v2",
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
            model = SentenceTransformer(self.embedding_model)
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


class ModelRouterPlugin:
    """模型路由插件 — 根据配置选择最优提供商和模型。

    配置参数:
        litellm_bridge: LiteLLMBridge 实例
    """

    name: str = "model_router"
    enabled: bool = True
    depends_on: list = []

    def __init__(self, litellm_bridge: Any = None) -> None:
        self.litellm_bridge = litellm_bridge

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        """执行模型路由决策。"""
        if self.litellm_bridge is None:
            return ctx

        model = ctx.request.get("model", "")
        if not model:
            return ctx

        # 路由决策：选择最佳 provider
        # 此处简化为记录所选模型，实际应实现负载均衡/成本优化逻辑
        ctx.selected_model = model
        ctx.model_router["selected_model"] = model

        logger.debug(
            "模型路由: model=%s, request_id=%s",
            model,
            ctx.request_id,
        )

        return ctx


class PromptCompressPlugin:
    """Prompt 压缩插件 — 在请求到达 LLM 前压缩冗长 prompt。

    配置参数:
        compression_ratio: 目标压缩比例，默认 0.5
    """

    name: str = "prompt_compress"
    enabled: bool = True
    depends_on: list = []

    def __init__(self, compression_ratio: float = 0.5) -> None:
        self.compression_ratio = compression_ratio

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        """执行 prompt 压缩。"""
        messages = ctx.request.get("messages", [])
        if not messages:
            return ctx

        # 提取总 token 数作为原始长度估算
        total_chars = sum(
            len(json.dumps(msg.get("content", ""))) for msg in messages
        )
        ctx.original_length = total_chars

        # TODO: 集成实际的 prompt 压缩逻辑（如 LangChain compression）
        # 目前仅记录原始长度，不做实际压缩
        ctx.compressed_prompt = ctx.request.get("messages", [])
        ctx.compression_ratio = 1.0

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

    plugin_map = {
        "pii_detector": (PIIDetectorPlugin, {"strategy": "sanitize"}),
        "prompt_cache": (PromptCachePlugin, {}),
        "semantic_cache": (SemanticCachePlugin, {}),
        "model_router": (ModelRouterPlugin, {}),
        "prompt_compress": (PromptCompressPlugin, {}),
    }

    for name, (plugin_cls, default_config) in plugin_map.items():
        # 查找配置
        cfg = None
        for pcfg in plugins_config:
            if isinstance(pcfg, dict) and pcfg.get("name") == name:
                cfg = pcfg
                break

        enabled = True
        priority = 0
        depends_on: list[str] = []
        plugin_config: dict = {}

        if cfg:
            enabled = cfg.get("enabled", True)
            priority = cfg.get("priority", 0)
            depends_on = cfg.get("depends_on", [])
            plugin_config = cfg.get("config", {})

        # 合并默认配置
        merged_config = {**default_config, **plugin_config}

        registry.register(
            name=name,
            plugin_class=plugin_cls,
            enabled=enabled,
            depends_on=depends_on,
            priority=priority,
            config=merged_config,
        )
