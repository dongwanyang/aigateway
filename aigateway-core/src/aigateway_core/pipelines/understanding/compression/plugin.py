"""Prompt compression plugin (LLMLingua-2) - understanding pipeline stage.

Token-level compression of the full prompt (system/history/user/RAG context).
Split out of the former ``prefix.plugins.classic_plugins`` module as part of
the 总分总 runtime split. When llmlingua is unavailable, degrades to
passthrough.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from aigateway_core.dispatch.context import PipelineContext

logger = logging.getLogger(__name__)


class PromptCompressPlugin:
    """Prompt 压缩插件 - LLMLingua-2 Token 级压缩。

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
        from aigateway_core.shared.integration_configs import PromptCompressConfig

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
