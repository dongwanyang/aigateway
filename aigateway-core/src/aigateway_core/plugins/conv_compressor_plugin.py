"""
ConvCompressorPlugin — 对话历史压缩插件
=========================================

使用 LangChain ConversationSummaryBufferMemory 将过长的对话历史
自动摘要压缩，减少上下文 Token 消耗同时保留对话语义。

当 langchain 包未安装或运行时异常时，自动降级为 passthrough 模式。

需求: 6.1, 6.2, 6.7
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

# 尝试导入 LangChain 依赖
try:
    from langchain.memory import ConversationSummaryBufferMemory
    from langchain_openai import ChatOpenAI

    _LANGCHAIN_AVAILABLE = True
except (ImportError, Exception):
    _LANGCHAIN_AVAILABLE = False


class ConvCompressorPlugin:
    """对话历史压缩插件 — LangChain ConversationSummaryBufferMemory。

    当对话消息数超过 max_history 阈值时，将旧消息通过 LLM 摘要压缩
    为简短摘要，保留最近 N 条消息不变。

    Attributes:
        name: 插件名称标识。
        enabled: 是否启用插件。
        depends_on: 依赖的上游插件列表。
    """

    name: str = "conv_compressor"
    enabled: bool = True
    depends_on: List[str] = ["semantic_cache"]

    def __init__(self, config: Optional[Any] = None) -> None:
        """初始化 ConvCompressorPlugin。

        Args:
            config: ConvCompressorConfig 实例。若为 None 则使用默认配置。
        """
        from ..integration_configs import ConvCompressorConfig

        if config is not None:
            self._config = config
        else:
            self._config = ConvCompressorConfig()

        self._is_available: bool = False
        self._memory: Optional[Any] = None
        self._llm: Optional[Any] = None
        self._initialize_memory()

    def _initialize_memory(self) -> None:
        """初始化 LangChain ConversationSummaryBufferMemory。

        如果 langchain 未安装或初始化失败，标记为不可用并进入 passthrough 模式。
        """
        if not _LANGCHAIN_AVAILABLE:
            self._is_available = False
            logger.warning(
                "langchain 或 langchain-openai 包未安装，"
                "ConvCompressorPlugin 将以 passthrough 模式运行。"
                "安装方式: pip install langchain langchain-openai"
            )
            return

        try:
            self._llm = ChatOpenAI(
                model=self._config.summary_model,
                temperature=0,
                max_tokens=self._config.max_token_limit,
            )
            self._memory = ConversationSummaryBufferMemory(
                llm=self._llm,
                max_token_limit=self._config.max_token_limit,
            )
            self._is_available = True
            logger.info(
                "ConvCompressorPlugin 已初始化: summary_model=%s, "
                "max_history=%d, max_token_limit=%d",
                self._config.summary_model,
                self._config.max_history,
                self._config.max_token_limit,
            )
        except Exception as exc:
            self._is_available = False
            logger.warning(
                "ConversationSummaryBufferMemory 初始化失败，"
                "降级为 passthrough: %s",
                exc,
            )

    async def execute(self, ctx: Any) -> Any:
        """执行对话历史压缩。

        流程:
        1. 如果 langchain 不可用，直接返回 ctx（passthrough）
        2. 获取 ctx.request["messages"] 中的对话历史
        3. 如果消息数 <= max_history，不压缩，直接返回
        4. 如果消息数 > max_history:
           a. 提取旧消息（除最后 max_history 条之外的消息）
           b. 使用 ConversationSummaryBufferMemory 对旧消息生成摘要
           c. 重建 messages: [system_msg (如有), summary_msg, 最近 N 条消息]
           d. 记录摘要到 ctx.extra["conv_compressor"]["summary"]
        5. 失败时 log warning 并透传原始消息

        Args:
            ctx: PipelineContext 管线上下文。

        Returns:
            修改后的 PipelineContext。
        """
        # passthrough: 包不可用
        if not self._is_available:
            return ctx

        messages = ctx.request.get("messages", [])
        if not messages:
            return ctx

        max_history = self._config.max_history

        # 消息数未达阈值，无需压缩
        if len(messages) <= max_history:
            return ctx

        # 需要压缩
        try:
            compressed_messages, raw_summary = await self._compress_messages(messages, max_history)

            # 清理内部标记
            for msg in compressed_messages:
                if "_is_summary" in msg:
                    del msg["_is_summary"]
                    break

            # 写回压缩后的消息
            ctx.request["messages"] = compressed_messages

            # 记录到 ctx.extra
            if not hasattr(ctx, "extra") or ctx.extra is None:
                ctx.extra = {}
            if "conv_compressor" not in ctx.extra:
                ctx.extra["conv_compressor"] = {}

            ctx.extra["conv_compressor"]["summary"] = raw_summary
            ctx.extra["conv_compressor"]["original_count"] = len(messages)
            ctx.extra["conv_compressor"]["compressed_count"] = len(compressed_messages)

            logger.debug(
                "对话历史压缩完成: original=%d messages, compressed=%d messages",
                len(messages),
                len(compressed_messages),
            )

        except Exception as exc:
            # 失败时透传原始消息
            logger.warning(
                "对话历史压缩失败，透传原始消息: %s", exc
            )

        return ctx

    async def _compress_messages(
        self, messages: List[dict], max_history: int
    ) -> tuple[List[dict], str]:
        """执行消息压缩逻辑。

        将旧消息通过 LangChain 摘要后，重建为:
        [system_msg (如有), summary_msg, 最近 max_history 条消息]

        Args:
            messages: 原始消息列表。
            max_history: 保留的最近消息数量。

        Returns:
            元组: (压缩后的消息列表, 原始摘要文本)。
        """
        # 分离 system 消息和其余消息
        system_msg: Optional[dict] = None
        non_system_messages: List[dict] = []

        for msg in messages:
            if msg.get("role") == "system" and system_msg is None:
                system_msg = msg
            else:
                non_system_messages.append(msg)

        # 确定需要压缩的旧消息和保留的近期消息
        recent_messages = non_system_messages[-max_history:]
        older_messages = non_system_messages[:-max_history]

        if not older_messages:
            # 没有需要压缩的旧消息
            return messages, ""

        # 使用 ConversationSummaryBufferMemory 生成摘要
        summary_text = await self._summarize_messages(older_messages)

        # 重建消息列表
        result: List[dict] = []

        # 1. 添加 system 消息（如有）
        if system_msg is not None:
            result.append(system_msg)

        # 2. 添加摘要消息
        if summary_text:
            result.append({
                "role": "system",
                "content": f"[对话历史摘要]: {summary_text}",
                "_is_summary": True,
            })

        # 3. 添加最近的消息
        result.extend(recent_messages)

        return result, summary_text

    async def _summarize_messages(self, messages: List[dict]) -> str:
        """使用 LangChain ConversationSummaryBufferMemory 对消息列表生成摘要。

        Args:
            messages: 需要摘要的旧消息列表。

        Returns:
            摘要文本字符串。
        """
        # 重置 memory 以避免跨请求状态污染
        self._memory.clear()

        # 将消息逐条加载到 memory 中
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if not content:
                continue

            if role in ("user", "system"):
                self._memory.chat_memory.add_user_message(content)
            elif role == "assistant":
                self._memory.chat_memory.add_ai_message(content)

        # 使用 predict_new_summary 生成摘要
        # ConversationSummaryBufferMemory 会自动对超出 token 限制的历史生成摘要
        buffer = self._memory.load_memory_variables({})

        # 提取摘要内容
        summary = buffer.get("history", "")
        if isinstance(summary, list):
            # 如果返回的是消息列表格式
            parts = []
            for item in summary:
                if hasattr(item, "content"):
                    parts.append(item.content)
                elif isinstance(item, str):
                    parts.append(item)
            summary = " ".join(parts)

        return summary
