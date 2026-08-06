"""Authenticated route for creating a video draft from an image draft result."""

from __future__ import annotations

import logging
from typing import Any, Literal

from aigateway_core.pipelines.generation._common.exceptions import (
    DraftWorkflowError,
)
from aigateway_core.pipelines.generation.draft.source_draft_video import (
    create_video_draft_from_source,
)
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .auth_middleware import authenticate_admin

logger = logging.getLogger(__name__)
router = APIRouter()


class SourceDraftVideoRequest(BaseModel):
    motion_prompt: str = Field(min_length=1, max_length=4000)
    duration_seconds: Literal[3, 5, 8] = 5
    fps: int = Field(default=8, ge=1, le=60, strict=True)
    chat_session_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )


class SourceDraftVideoResponse(BaseModel):
    request_id: str
    source_draft_id: str
    draft_id: str
    status: str
    media_type: Literal["video"] = "video"
    preview_url: str
    source_image_sha256: str
    duration_seconds: float
    fps: int
    frame_count: int
    expires_at: float


def _strategy(request: Request) -> Any:
    strategy = getattr(request.app.state, "draft_strategy", None)
    if strategy is None:
        strategy = getattr(request.app.state, "draft_generator_strategy", None)
    if strategy is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "code": "draft_unavailable",
                    "message": "Draft workflow service is not available.",
                }
            },
        )
    return strategy


def _request_id(request: Request) -> str:
    return str(
        request.headers.get("X-Request-ID")
        or getattr(request.state, "request_id", "")
        or getattr(request.state, "trace_id", "")
        or ""
    ).strip()


def _error_response(error: str) -> tuple[int, str, str]:
    code = error.split(":", 1)[0].strip() or "internal_error"
    exact: dict[str, tuple[int, str]] = {
        "source_draft_not_found": (
            404,
            "原图片草稿不存在、已过期或结果文件已清理。",
        ),
        "source_draft_forbidden": (403, "无权使用该图片草稿。"),
        "source_draft_invalid_type": (
            409,
            "仅支持使用已完成的图片结果创建视频。",
        ),
        "video_duration_unsupported": (
            422,
            "当前模型不支持该视频时长或帧率。",
        ),
        "video_motion_prompt_missing": (
            422,
            "请描述主体动作或镜头运动。",
        ),
        "source_draft_immutable": (
            409,
            "来源图片视频草稿不可重新生成关键帧。",
        ),
    }
    if code in exact:
        status, message = exact[code]
        return status, code, message

    unavailable_prefixes = (
        "config_missing",
        "comfyui_missing_dependencies",
        "comfyui_video_not_enabled",
        "comfyui_video_workflow_invalid",
        "comfyui_invalid_video_model",
        "comfyui_invalid_video_text_encoder",
        "comfyui_invalid_video_vae",
        "gpu_scheduler",
    )
    if code.startswith(unavailable_prefixes):
        return (
            503,
            code,
            "本地视频生成服务当前不可用或依赖未就绪。",
        )
    if (
        "ComfyUI service is unavailable" in error
        or "ComfyUI health check failed" in error
    ):
        return (
            503,
            "comfyui_unavailable",
            "本地视频生成服务当前不可用或依赖未就绪。",
        )
    resource_prefixes = (
        "comfyui_storage_",
        "comfyui_output_budget_",
        "comfyui_model_budget_",
    )
    if code.startswith(resource_prefixes):
        return 507, code, "本地生成存储空间或模型预算不足。"
    return 500, "internal_error", "创建视频草稿时发生内部错误。"


def _domain_http_exception(
    exc: BaseException,
    *,
    source_draft_id: str,
    chat_session_id: str,
) -> HTTPException:
    status, code, message = _error_response(str(exc))
    context = {
        "source_draft_id": source_draft_id,
        "chat_session_id": chat_session_id,
        "error_code": code,
    }
    if status >= 500:
        logger.error("source_draft_video.create_failed", extra=context)
    else:
        logger.warning("source_draft_video.create_rejected", extra=context)
    return HTTPException(
        status_code=status,
        detail={"error": {"code": code, "message": message}},
    )


def _is_reloaded_draft_workflow_error(exc: BaseException) -> bool:
    return type(exc).__name__ == "DraftWorkflowError"


@router.post(
    "/draft/{source_draft_id}/video",
    response_model=SourceDraftVideoResponse,
)
async def create_video_from_source_draft(
    source_draft_id: str,
    body: SourceDraftVideoRequest,
    request: Request,
    auth: dict[str, Any] = Depends(authenticate_admin),
) -> SourceDraftVideoResponse:
    """Create a frozen video draft from an authorized completed image result."""
    strategy = _strategy(request)
    stable_request_id = _request_id(request)
    try:
        draft = await create_video_draft_from_source(
            strategy,
            source_draft_id=source_draft_id,
            motion_prompt=body.motion_prompt,
            duration_seconds=body.duration_seconds,
            fps=body.fps,
            chat_session_id=body.chat_session_id,
            user_id=str(auth.get("user_id") or "") or None,
            group_id=str(auth.get("group_id") or "") or None,
            trace_id=str(getattr(request.state, "trace_id", "") or stable_request_id),
            request_id=stable_request_id or None,
        )
    except HTTPException:
        raise
    except DraftWorkflowError as exc:
        raise _domain_http_exception(
            exc,
            source_draft_id=source_draft_id,
            chat_session_id=body.chat_session_id,
        ) from exc
    except Exception as exc:
        if _is_reloaded_draft_workflow_error(exc):
            raise _domain_http_exception(
                exc,
                source_draft_id=source_draft_id,
                chat_session_id=body.chat_session_id,
            ) from exc
        logger.exception(
            "source_draft_video.create_unhandled",
            extra={
                "source_draft_id": source_draft_id,
                "chat_session_id": body.chat_session_id,
            },
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error": {
                    "code": "internal_error",
                    "message": "创建视频草稿时发生内部错误。",
                }
            },
        ) from exc

    params = draft.generation_params
    return SourceDraftVideoResponse(
        request_id=str(params.get("request_id") or stable_request_id),
        source_draft_id=source_draft_id,
        draft_id=draft.draft_id,
        status=draft.status,
        preview_url=f"/admin/draft/{draft.draft_id}/preview",
        source_image_sha256=str(params["source_image_sha256"]),
        duration_seconds=float(params["duration_seconds"]),
        fps=int(params["fps"]),
        frame_count=int(params["frame_count"]),
        expires_at=draft.expires_at,
    )


def install_source_draft_video_routes(admin_router: APIRouter) -> None:
    marker = "_aigateway_source_draft_video_routes_installed"
    if getattr(admin_router, marker, False):
        return
    admin_router.include_router(router)
    setattr(admin_router, marker, True)


__all__ = ["install_source_draft_video_routes", "router"]
