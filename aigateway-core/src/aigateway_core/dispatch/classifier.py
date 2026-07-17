"""请求分类 —— 意图驱动路由.

classify_request 调 IntentClassifier(LLM 预判)输出带媒介 pipeline_kind:
"understanding" | "generation:image" | "generation:video".
取消 generation_intent 字段、模型名推断、auto 魔法字符串。
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)


async def classify_request(
    body: Any,
    config_manager: Any,
    intent_classifier: Optional[Any] = None,
) -> Tuple[str, Optional[str]]:
    """把请求分类为 understanding | generation:image | generation:video.

    Args:
        body: ChatCompletionRequest(有 .model/.messages 属性)或 dict.
        config_manager: 配置管理器(保留参数, 当前未用).
        intent_classifier: IntentClassifier 实例. None 时默认 understanding.

    Returns:
        (pipeline_kind, model_hint) 二元组。model_hint 为预判/客户端指定的
        模型名(裸名)或 None,由 dispatcher 透传给 bridge。不写入 body,避免污染
        入参 Pydantic 对象(否则 body 被序列化/缓存/日志会带上内部字段)。
    """
    messages = getattr(body, "messages", None)
    if messages is None and isinstance(body, dict):
        messages = body.get("messages")

    if intent_classifier is None:
        logger.debug("classify_request: 无 intent_classifier, 默认 understanding")
        return "understanding", None

    model = getattr(body, "model", None)
    if model is None and isinstance(body, dict):
        model = body.get("model")

    try:
        result = await intent_classifier.classify(messages=messages or [], body_model=model)
    except Exception as exc:
        logger.warning("classify_request: intent_classifier 异常 %s, 默认 understanding", exc)
        return "understanding", None

    generation = result.get("generation", "understanding")
    hint = result.get("hint", "None")
    model_hint = hint if hint != "None" else None

    if generation == "image":
        return "generation:image", model_hint
    if generation == "video":
        return "generation:video", model_hint
    return "understanding", model_hint
