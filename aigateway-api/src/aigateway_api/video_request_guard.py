"""Request-bound guards for progressive video generation semantics."""
from __future__ import annotations

import functools
import re
from collections.abc import Mapping, Sequence
from typing import Any

from fastapi.responses import JSONResponse

from aigateway_core.dispatch.dispatcher import RequestDispatcher

_ORIGINAL_ATTR = "_aigateway_original_video_request_guard_dispatch"
_GUARD_ATTR = "_aigateway_video_request_guard"

_ZH_REFERENCE_RE = re.compile(
    r"(?:根据|基于|使用|用|把|让)?\s*(?:这|该|上面|刚才|之前|上一)(?:一)?(?:张|个)?\s*(?:图|图片|照片|画面)"
)
_EN_REFERENCE_RE = re.compile(
    r"\b(?:this|that|the\s+above|previous|last)\s+(?:image|picture|photo|frame)\b",
    re.IGNORECASE,
)
_ZH_VIDEO_GENERATION_RE = re.compile(
    r"(?:图生视频|生成视频|制作视频|创建视频|视频生成|"
    r"(?:生成|制作|创建|转成|转换成|转换为|做成|变成).{0,16}(?:视频|动画)|"
    r"(?:让|使).{0,16}(?:动起来|运动起来)|动起来)"
)
_EN_VIDEO_GENERATION_RE = re.compile(
    r"(?:\banimate\b|"
    r"\b(?:generate|create|make|turn|convert)\b.{0,60}\b(?:video|animation)\b|"
    r"\b(?:video|animation)\b.{0,60}\b(?:generate|create|make)\b|"
    r"\bmake\b.{0,40}\b(?:move|moving)\b)",
    re.IGNORECASE,
)


def _value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _generation_options(body: Any) -> Mapping[str, Any]:
    raw = _value(body, "generation_options", {})
    if isinstance(raw, Mapping):
        return raw
    dump = getattr(raw, "model_dump", None)
    if callable(dump):
        value = dump(exclude_none=True)
        return value if isinstance(value, Mapping) else {}
    return {}


def _latest_user_turn(body: Any) -> tuple[str, bool]:
    messages = _value(body, "messages", [])
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        return "", False
    for message in reversed(messages):
        if _value(message, "role") != "user":
            continue
        content = _value(message, "content", "")
        if isinstance(content, str):
            return content, False
        if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
            return "", False
        text_parts: list[str] = []
        has_image = False
        for part in content:
            part_type = _value(part, "type", "")
            if part_type == "text":
                text = _value(part, "text", "")
                if isinstance(text, str):
                    text_parts.append(text)
            elif part_type == "image_url":
                image = _value(part, "image_url", None)
                url = _value(image, "url", image)
                has_image = has_image or bool(isinstance(url, str) and url.strip())
        return "\n".join(text_parts), has_image
    return "", False


def reference_image_required(body: Any) -> bool:
    """Return whether an explicit image reference is missing for a video request."""
    options = _generation_options(body)
    source_draft_id = options.get("source_draft_id") or _value(body, "source_draft_id", None)
    if isinstance(source_draft_id, str) and source_draft_id.strip():
        return False

    text, has_image = _latest_user_turn(body)
    if has_image or not text.strip():
        return False
    references_image = bool(_ZH_REFERENCE_RE.search(text) or _EN_REFERENCE_RE.search(text))
    requests_video_generation = bool(
        _ZH_VIDEO_GENERATION_RE.search(text)
        or _EN_VIDEO_GENERATION_RE.search(text)
    )
    return references_image and requests_video_generation


def _reference_image_error() -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "code": "reference_image_required",
                "message": "未找到参考图片，请上传图片或从图片结果点击“基于此图生成视频”。",
            }
        },
    )


def install_video_request_guard() -> None:
    """Install one idempotent dispatcher wrapper before pipeline execution."""
    current = RequestDispatcher.dispatch
    if getattr(current, _GUARD_ATTR, False):
        return
    if not hasattr(RequestDispatcher, _ORIGINAL_ATTR):
        setattr(RequestDispatcher, _ORIGINAL_ATTR, current)
    original = getattr(RequestDispatcher, _ORIGINAL_ATTR)

    @functools.wraps(original)
    async def guarded_dispatch(self: Any, body: Any, request: Any) -> Any:
        if reference_image_required(body):
            return _reference_image_error()
        return await original(self, body, request)

    setattr(guarded_dispatch, _GUARD_ATTR, True)
    RequestDispatcher.dispatch = guarded_dispatch


__all__ = ["install_video_request_guard", "reference_image_required"]
