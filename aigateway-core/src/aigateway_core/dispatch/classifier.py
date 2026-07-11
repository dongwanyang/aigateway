from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _has_multimodal_content(messages: list) -> bool:
    """messages 是否含 list 类型 content（多模态：图片/音频/视频）。"""
    for message in messages or []:
        if isinstance(message, dict) and isinstance(message.get("content"), list):
            return True
    return False


def _content_modality_hint(messages: list) -> Optional[str]:
    """从多模态 content 推断模态倾向。"""
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict):
                block_type = block.get("type", "")
                if block_type in ("image_url", "input_image", "image"):
                    return "generation"
                if block_type in ("input_audio", "audio"):
                    return "generation"
                if block_type in ("video", "input_video"):
                    return "generation"
    return None


def classify_request(body: Any, config_manager: Any) -> str:
    """把请求分类为 understanding | generation。"""
    model = getattr(body, "model", None) or (body.get("model") if isinstance(body, dict) else None)
    messages = getattr(body, "messages", None) or (body.get("messages") if isinstance(body, dict) else None)
    generation_intent = getattr(body, "generation_intent", None)
    if generation_intent is None and isinstance(body, dict):
        generation_intent = body.get("generation_intent")

    if generation_intent is True:
        return "generation"

    if messages and _content_modality_hint(messages) == "generation":
        return "generation"

    if model and model != "auto" and config_manager is not None:
        try:
            providers = config_manager.get("providers", {}) or {}
            for provider in providers.values():
                if not isinstance(provider, dict):
                    continue
                for group in provider.get("model_grouper", []) or []:
                    if not isinstance(group, dict):
                        continue
                    for configured_model in group.get("models", []) or []:
                        if isinstance(configured_model, dict) and configured_model.get("name") == model:
                            modalities = configured_model.get("modalities") or configured_model.get("modality")
                            if modalities:
                                if isinstance(modalities, str):
                                    modalities = [modalities]
                                if "generative" in modalities or "image" in modalities or "video" in modalities:
                                    return "generation"
                            return "understanding"
        except Exception as exc:
            logger.warning("classify_request 模型推断异常: %s", exc)

    return "understanding"
