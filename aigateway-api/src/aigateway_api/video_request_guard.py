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
_PENDING_REQUEST_TTL_SECONDS = 300

_ZH_REFERENCE_RE = re.compile(
    r"(?:根据|基于|使用|用|把|让|以|拿)?\s*"
    r"(?:这|该|此|当前|上面|刚才|刚刚|之前|上一|刚生成(?:的)?|刚刚生成(?:的)?)"
    r"(?:一)?(?:张|个)?\s*(?:图|图片|照片|画面|图像|结果)"
)
_EN_REFERENCE_RE = re.compile(
    r"\b(?:this|that|the\s+above|current|previous|last|just[-\s]+generated)\s+"
    r"(?:image|picture|photo|frame|result)\b",
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


def _source_draft_id(body: Any) -> str:
    value = _generation_options(body).get("source_draft_id")
    return value.strip() if isinstance(value, str) else ""


def reference_image_required(body: Any) -> bool:
    """Return whether an explicit existing-image reference is missing."""
    if _source_draft_id(body):
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


def _draft_strategy(request: Any) -> Any | None:
    strategy = getattr(request.app.state, "draft_strategy", None)
    if strategy is None:
        strategy = getattr(request.app.state, "draft_generator_strategy", None)
    return strategy


async def _register_pending_request(body: Any, request: Any) -> None:
    """Bind request identity to owner/session before draft creation can race Stop."""
    strategy = _draft_strategy(request)
    register = getattr(strategy, "register_request_draft", None)
    if not callable(register):
        return
    request_id = str(getattr(request.state, "request_id", "") or "")
    session_id = str(_value(body, "chat_session_id", "") or "")
    owner = getattr(request.state, "draft_owner", None)
    owner = owner if isinstance(owner, Mapping) else {}
    user_id = str(owner.get("user_id") or "") or None
    group_id = str(owner.get("group_id") or "") or None
    if not request_id or not session_id or (not user_id and not group_id):
        return
    await register(
        request_id,
        "",
        user_id=user_id,
        group_id=group_id,
        session_id=session_id,
        ttl_seconds=_PENDING_REQUEST_TTL_SECONDS,
    )


async def _create_source_draft_response(
    body: Any,
    request: Any,
    source_draft_id: str,
) -> JSONResponse:
    from aigateway_core.pipelines.generation._common.exceptions import (
        DraftWorkflowError,
    )
    from aigateway_core.pipelines.generation.draft.source_draft_video import (
        create_video_draft_from_source,
    )

    from .source_draft_video_routes import _domain_http_exception

    strategy = _draft_strategy(request)
    if strategy is None:
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "draft_unavailable",
                    "message": "Draft workflow service is not available.",
                }
            },
        )

    motion_prompt, _ = _latest_user_turn(body)
    options = _generation_options(body)
    session_id = str(_value(body, "chat_session_id", "") or "")
    owner = getattr(request.state, "draft_owner", None)
    owner = owner if isinstance(owner, Mapping) else {}
    request_id = str(getattr(request.state, "request_id", "") or "")
    trace_id = str(getattr(request.state, "trace_id", "") or request_id)
    try:
        draft = await create_video_draft_from_source(
            strategy,
            source_draft_id=source_draft_id,
            motion_prompt=motion_prompt,
            duration_seconds=options.get("duration_seconds", 5),
            fps=options.get("fps", 8),
            chat_session_id=session_id,
            user_id=str(owner.get("user_id") or "") or None,
            group_id=str(owner.get("group_id") or "") or None,
            trace_id=trace_id,
            request_id=request_id or None,
        )
    except DraftWorkflowError as exc:
        raise _domain_http_exception(
            exc,
            source_draft_id=source_draft_id,
            chat_session_id=session_id,
        ) from exc

    return JSONResponse(
        content={
            "data": {
                "draft_id": draft.draft_id,
                "preview_url": f"/admin/draft/{draft.draft_id}/preview",
                "status": draft.status,
                "generation_params": draft.generation_params,
            },
            "_meta": {"draft_pending_confirmation": True},
        }
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
        await _register_pending_request(body, request)
        source_draft_id = _source_draft_id(body)
        if source_draft_id:
            return await _create_source_draft_response(
                body,
                request,
                source_draft_id,
            )
        if reference_image_required(body):
            return _reference_image_error()
        return await original(self, body, request)

    setattr(guarded_dispatch, _GUARD_ATTR, True)
    RequestDispatcher.dispatch = guarded_dispatch


__all__ = ["install_video_request_guard", "reference_image_required"]
