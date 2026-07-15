"""请求分类 —— 意图驱动路由.

classify_request 调 IntentClassifier(LLM 预判)输出带媒介 pipeline_kind:
"understanding" | "generation:image" | "generation:video".
取消 generation_intent 字段、模型名推断、auto 魔法字符串。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


async def classify_request(
    body: Any,
    config_manager: Any,
    intent_classifier: Optional[Any] = None,
) -> str:
    """把请求分类为 understanding | generation:image | generation:video.

    Args:
        body: ChatCompletionRequest(有 .model/.messages 属性)或 dict.
        config_manager: 配置管理器(保留参数, 当前未用).
        intent_classifier: IntentClassifier 实例. None 时默认 understanding.

    Returns:
        pipeline_kind 字符串.
    """
    messages = getattr(body, "messages", None)
    if messages is None and isinstance(body, dict):
        messages = body.get("messages")

    if intent_classifier is None:
        logger.debug("classify_request: 无 intent_classifier, 默认 understanding")
        return "understanding"

    model = getattr(body, "model", None)
    if model is None and isinstance(body, dict):
        model = body.get("model")

    try:
        result = await intent_classifier.classify(messages=messages or [], body_model=model)
    except Exception as exc:
        logger.warning("classify_request: intent_classifier 异常 %s, 默认 understanding", exc)
        return "understanding"

    generation = result.get("generation", "understanding")
    hint = result.get("hint", "None")

    # 把 hint 存到 body 上, 供 dispatcher 传给 bridge 作 model_hint
    try:
        setattr(body, "_intent_hint", hint if hint != "None" else None)
    except Exception:
        pass

    if generation == "image":
        return "generation:image"
    if generation == "video":
        return "generation:video"
    return "understanding"
