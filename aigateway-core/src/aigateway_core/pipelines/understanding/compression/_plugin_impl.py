"""Prompt compression plugin (LLMLingua-2) - understanding pipeline stage.

Token-level compression of the full prompt (system/history/user/RAG context).
Split out of the former ``prefix.plugins.classic_plugins`` module as part of
the 总分总 runtime split. When llmlingua is unavailable, degrades to
passthrough.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from aigateway_core.dispatch.context import PipelineContext
from aigateway_core.shared.integration_configs import PromptCompressConfig

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
        config: PromptCompressConfig | None = None,
        *,
        compression_ratio: float = 0.5,
    ) -> None:
        if config is not None:
            self._config = config
        else:
            self._config = PromptCompressConfig(compression_ratio=compression_ratio)

        self._compressor: Any = None
        self._is_available: bool = False
        self._initialized: bool = False
        self._condition = threading.Condition(threading.RLock())
        self._active = 0
        self._releasing = False
        self._configured_device_request = (
            self._config.device or "cpu"
        ).strip().lower()
        self._runtime_device = self._configured_device_request

    @property
    def gpu_device_request(self) -> str:
        return self._configured_device_request

    def set_runtime_device(self, device: str) -> None:
        if device == self._runtime_device:
            return
        released = self.release_if_idle()
        if released["busy"]:
            raise RuntimeError("prompt_compressor_busy")
        self._runtime_device = device

    def release_if_idle(self) -> dict[str, bool]:
        """Delete the local compressor only when no inference is active."""
        with self._condition:
            if self._active or self._releasing:
                return {"released": False, "busy": True}
            if self._compressor is None:
                self._initialized = False
                return {"released": False, "busy": False}
            self._releasing = True
            self._compressor = None
            self._is_available = False
            self._initialized = False
            self._releasing = False
            self._condition.notify_all()
        return {"released": True, "busy": False}

    def _ensure_compressor_loaded(self, *, load_if_missing: bool = True) -> None:
        """Initialize LLMLingua-2 when explicitly allowed.

        Request-time execution passes ``load_if_missing=False`` so a cold
        compression model cannot block the single gateway worker.
        """
        if self._initialized:
            return
        if not load_if_missing:
            return
        self._initialized = True
        self._init_compressor()

    def _init_compressor(self) -> None:
        """延迟初始化 LLMLingua-2 压缩器。ImportError 时标记 passthrough。"""
        try:
            from llmlingua import PromptCompressor

            device_map = self._runtime_device
            if device_map not in ("cpu", "cuda", "auto") and not (
                device_map.startswith("cuda:") and device_map[5:].isdigit()
            ):
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

        await asyncio.to_thread(
            self._ensure_compressor_loaded, load_if_missing=True
        )

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
            with self._condition:
                while self._releasing:
                    self._condition.wait()
                compressor = self._compressor
                self._active += 1
            try:
                result = await asyncio.to_thread(
                    compressor.compress_prompt,
                    prompt_text,
                    rate=self._config.compression_ratio,
                    target_token=(
                        self._config.target_token
                        if self._config.target_token > 0
                        else -1
                    ),
                    force_tokens=self._config.force_tokens,
                )
            finally:
                with self._condition:
                    self._active -= 1
                    self._condition.notify_all()
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
