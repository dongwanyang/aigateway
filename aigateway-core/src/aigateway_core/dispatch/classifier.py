"""请求分类 —— 意图驱动路由.

classify_request 调 IntentClassifier(LLM 预判)输出带媒介 pipeline_kind:
"understanding" | "generation:image" | "generation:video".
取消 generation_intent 字段、模型名推断、auto 魔法字符串。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterator, Optional

from aigateway_core.route.model_resolution.task_classifier import TaskProfile

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClassificationResult:
    """Backward-compatible classification result.

    Iteration yields the historical ``(pipeline_kind, model_hint)`` pair while
    the dispatcher can consume the validated Task Profile.
    """

    pipeline_kind: str
    model_hint: Optional[str] = None
    task_profile: Optional[TaskProfile] = None
    source: str = "fallback"

    def __iter__(self) -> Iterator[Any]:
        yield self.pipeline_kind
        yield self.model_hint


async def classify_request(
    body: Any,
    config_manager: Any,
    intent_classifier: Optional[Any] = None,
) -> ClassificationResult:
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
        return ClassificationResult(
            "understanding", task_profile=TaskProfile(), source="no_classifier"
        )

    model = getattr(body, "model", None)
    if model is None and isinstance(body, dict):
        model = body.get("model")

    try:
        result = await intent_classifier.classify(
            messages=messages or [],
            body_model=model,
            tools=getattr(body, "tools", None),
            structured_output=bool(getattr(body, "response_format", None)),
        )
    except Exception as exc:
        logger.warning("classify_request: intent_classifier 异常 %s, 默认 understanding", exc)
        return ClassificationResult(
            "understanding", task_profile=TaskProfile(), source="classifier_error"
        )

    generation = result.get("generation", "understanding")
    hint = result.get("hint", "None")
    model_hint = hint if hint != "None" else None
    try:
        task_profile = TaskProfile.from_dict(
            result.get("task_profile"),
            source=str(result.get("classification_source", "llm")),
        )
    except ValueError:
        task_profile = TaskProfile(source="invalid_profile_fallback")
    source = str(result.get("classification_source", task_profile.source))

    if generation == "image":
        return ClassificationResult(
            "generation:image", model_hint, task_profile, source
        )
    if generation == "video":
        return ClassificationResult(
            "generation:video", model_hint, task_profile, source
        )
    return ClassificationResult("understanding", model_hint, task_profile, source)
