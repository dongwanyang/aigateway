"""
GenerationPipeline — Generation Pipeline + Prompt Enhancement
==============================================================

封装 LLM 调用前的 Prompt Enhancement 和模型选择。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .config import GenerationConfig

if TYPE_CHECKING:
    from ..context import PipelineContext

logger = logging.getLogger(__name__)


class PromptEnhancer:
    """Prompt Enhancement — 增强用户输入以提高响应质量。

    级别:
    - off: 不增强，原样透传
    - light: 添加格式化指令
    - aggressive: 完整重写 prompt（CoT 注入）
    """

    def __init__(self, level: str = "off") -> None:
        self.level = level  # "off" | "light" | "aggressive"

    async def enhance(
        self, request: Dict[str, Any], ctx: "PipelineContext"
    ) -> Dict[str, Any]:
        """增强请求。"""
        if self.level == "off":
            return request

        messages = list(request.get("messages", []))
        if not messages:
            return request

        if self.level == "light":
            if not any(m.get("role") == "system" for m in messages):
                messages.insert(0, {
                    "role": "system",
                    "content": "Provide clear, structured responses.",
                })

        elif self.level == "aggressive":
            # 注入 Chain-of-Thought
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "user":
                    content = messages[i].get("content", "")
                    if isinstance(content, str):
                        messages[i] = {
                            **messages[i],
                            "content": f"{content}\n\nPlease think step by step.",
                        }
                    break

        # 记录到 context
        gen_ns = ctx.extra.setdefault("generation_pipeline", {})
        gen_ns["enhancement_level"] = self.level

        return {**request, "messages": messages}


class GenerationPipeline:
    """Generation Pipeline — 负责 LLM 调用前后的完整流程。

    职责:
    1. Prompt Enhancement
    2. Model Selection（基于内容类型）
    3. LLM Completion（通过 LiteLLM Bridge）
    """

    def __init__(
        self,
        litellm_bridge: Any = None,
        config: Optional[GenerationConfig] = None,
    ) -> None:
        cfg = config or GenerationConfig()
        self._litellm = litellm_bridge
        self._config = cfg
        self._prompt_enhancer = PromptEnhancer(level=cfg.enhancement_level)

    async def generate(self, ctx: "PipelineContext") -> Optional[str]:
        """执行 Generation Pipeline。

        Args:
            ctx: 经过 Media Optimization 后的 Pipeline Context。

        Returns:
            LLM 响应文本，或 None（如果无法生成）。
        """
        if self._litellm is None:
            return None

        # Step 1: Prompt Enhancement
        enhanced_request = await self._prompt_enhancer.enhance(ctx.request, ctx)

        # Step 2: Model Selection
        model = self._select_model(ctx)
        enhanced_request["model"] = model

        # Step 3: LLM Completion
        try:
            response = await self._litellm.completion(
                messages=enhanced_request["messages"],
                model=model,
                stream=ctx.should_stream,
                **self._extract_params(enhanced_request),
            )

            # 记录到 context
            gen_ns = ctx.extra.setdefault("generation_pipeline", {})
            gen_ns["selected_model"] = model
            gen_ns["prompt_enhanced"] = True

            return response
        except Exception as exc:
            logger.error("Generation Pipeline LLM 调用失败: %s", exc)
            return None

    def _select_model(self, ctx: "PipelineContext") -> str:
        """基于多模态内容类型选择最优模型。"""
        is_multimodal = ctx.extra.get("media_optimization", {}).get(
            "detected_types", []
        )
        if is_multimodal:
            return self._config.vision_model or "gpt-4o"
        return ctx.request.get("model", self._config.default_model)

    def _extract_params(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """提取 LLM 调用参数。"""
        return {
            k: v
            for k, v in request.items()
            if k in ("temperature", "max_tokens", "top_p", "frequency_penalty")
        }
