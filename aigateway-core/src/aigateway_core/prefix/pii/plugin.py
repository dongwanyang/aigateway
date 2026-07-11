"""PII detection plugin - shared prefix stage.

Runs PIIDetector over request messages and sanitizes/rejects/hashes sensitive
content before pipeline dispatch. Split out of the former
``prefix.plugins.classic_plugins`` module as part of the 总分总 runtime split.
"""
from __future__ import annotations

import logging

from aigateway_core.dispatch.context import PipelineContext
from aigateway_core.prefix.pii.detector import PIIDetector

logger = logging.getLogger(__name__)


class PIIDetectorPlugin:
    """PII 检测插件 - 在请求到达 LLM 前扫描并脱敏敏感信息。

    执行流程:
    1. 从 request.messages 中提取文本内容
    2. 使用 PIIDetector 进行三遍检测（exclusion -> named-field -> standalone）
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

        # Collect the text of every message (string content, or the joined
        # text blocks of list content) for detection. We keep the per-message
        # text so sanitization can be written back to each message
        # individually — overwriting every message with one concatenated
        # sanitized blob (the old behavior) corrupts multi-turn conversations.
        per_msg_texts: list[str] = []
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                per_msg_texts.append(content)
            elif isinstance(content, list):
                parts = [
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                per_msg_texts.append("\n".join(parts))
            else:
                per_msg_texts.append("")

        if not any(t.strip() for t in per_msg_texts):
            return ctx

        full_text = "\n".join(per_msg_texts)

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

        # Only rewrite messages if PII was actually detected/scrubbed.
        if sanitized != full_text and self.detector.detected_categories:
            updated = list(messages)
            # Sanitize each message's text individually so PII in one message
            # doesn't bleed surrounding context into another.
            for i, msg in enumerate(updated):
                content = msg.get("content", "")
                if isinstance(content, str):
                    if content.strip():
                        updated[i] = {**msg, "content": self.detector.process(content)}
                elif isinstance(content, list):
                    new_content = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            block_text = block.get("text", "")
                            new_content.append(
                                {**block, "text": self.detector.process(block_text)}
                                if block_text else block
                            )
                        else:
                            new_content.append(block)
                    updated[i] = {**msg, "content": new_content}
            ctx.request["messages"] = updated

        if self.detector.detected_categories:
            logger.info(
                "PII 检测完成: categories=%s, strategy=%s, request_id=%s",
                self.detector.detected_categories,
                self.detector.strategy,
                ctx.request_id,
            )

        return ctx
