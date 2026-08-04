"""
Draft Routes — Draft-to-HiRes 工作流 API 端点
==============================================

提供草稿确认、拒绝以及“已有图片转视频”端点。
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/generation", tags=["generation-optimization"])


# ==================================================================
# 请求/响应模型
# ==================================================================


class DraftActionRequest(BaseModel):
    """草图操作请求."""

    action: str

    @field_validator("action")
    @classmethod
    def validate_action(cls, v: str) -> str:
        if v not in ("confirm", "reject"):
            raise ValueError(
                f"Invalid action '{v}'. Must be 'confirm' or 'reject'."
            )
        return v


class DraftActionResponse(BaseModel):
    """草图操作响应."""

    draft_id: str
    action: str
    status: str
    data: dict[str, Any] = Field(default_factory=dict)


class SourceDraftVideoRequest(BaseModel):
    """Create a video draft from an authorized completed image draft."""

    motion_prompt: str = Field(min_length=1, max_length=4000)
    duration_seconds: Literal[3, 5, 8] = 5
    fps: int = Field(default=8, ge=1, le=60, strict=True)
    chat_session_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )


class SourceDraftVideoResponse(BaseModel):
    """New frozen video draft created from an image result."""

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


# ==================================================================
# API 端点
# ==================================================================


@router.post(
    "/drafts/{source_draft_id}/video",
    response_model=SourceDraftVideoResponse,
)
async def create_video_from_source_draft(
    source_draft_id: str,
    request_body: SourceDraftVideoRequest,
    request: Request,
) -> SourceDraftVideoResponse:
    """Copy a completed image result into a new confirmable video draft."""
    strategy = _get_draft_strategy(request)
    if strategy is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "code": "service_unavailable",
                    "message": "Draft workflow service is not available.",
                }
            },
        )

    from aigateway_core.pipelines.generation.draft.source_draft_video import (
        create_video_draft_from_source,
    )

    user_id, group_id = _request_owner(request)
    trace_id = str(getattr(request.state, "trace_id", "") or "")
    try:
        draft = await create_video_draft_from_source(
            strategy,
            source_draft_id=source_draft_id,
            motion_prompt=request_body.motion_prompt,
            duration_seconds=request_body.duration_seconds,
            fps=request_body.fps,
            chat_session_id=request_body.chat_session_id,
            user_id=user_id,
            group_id=group_id,
            trace_id=trace_id,
        )
    except HTTPException:
        raise
    except Exception as exc:
        code = str(exc) or "internal_error"
        status_code, message = _source_video_error(code)
        if status_code >= 500:
            logger.exception(
                "generation_optimization.source_draft_video.error",
                extra={
                    "source_draft_id": source_draft_id,
                    "chat_session_id": request_body.chat_session_id,
                    "error": code,
                },
            )
        else:
            logger.warning(
                "generation_optimization.source_draft_video.rejected",
                extra={
                    "source_draft_id": source_draft_id,
                    "chat_session_id": request_body.chat_session_id,
                    "error_code": code,
                },
            )
        raise HTTPException(
            status_code=status_code,
            detail={"error": {"code": code, "message": message}},
        ) from exc

    params = draft.generation_params
    return SourceDraftVideoResponse(
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


@router.post("/drafts/{draft_id}/action", response_model=DraftActionResponse)
async def draft_action(
    draft_id: str,
    request_body: DraftActionRequest,
    request: Request,
) -> DraftActionResponse:
    """执行草图确认或拒绝操作."""
    action = request_body.action
    strategy = _get_draft_strategy(request)

    if strategy is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "code": "service_unavailable",
                    "message": "Draft workflow service is not available.",
                }
            },
        )

    try:
        if action == "confirm":
            result = await strategy.confirm_draft(draft_id)
            return DraftActionResponse(
                draft_id=draft_id,
                action="confirm",
                status="confirmed",
                data={
                    "target_resolution": list(result.target_resolution),
                    "algorithm_used": result.algorithm_used,
                    "duration_ms": round(result.duration_ms, 2),
                },
            )

        if action == "reject":
            new_draft = await strategy.reject_draft(draft_id)
            return DraftActionResponse(
                draft_id=new_draft.draft_id,
                action="reject",
                status="regenerated",
                data={
                    "new_draft_id": new_draft.draft_id,
                    "preview_count": len(new_draft.previews),
                    "attempt_number": new_draft.attempt_number,
                    "max_attempts": new_draft.max_attempts,
                    "expires_at": new_draft.expires_at,
                },
            )

        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "code": "invalid_action",
                    "message": f"Invalid action '{action}'. Must be 'confirm' or 'reject'.",
                }
            },
        )

    except HTTPException:
        raise
    except Exception as exc:
        error_msg = str(exc)
        upstream_status = getattr(exc, "upstream_status", None)
        upstream_unavailable = getattr(exc, "upstream_unavailable", False)
        if (isinstance(upstream_status, int) and upstream_status >= 500) or upstream_unavailable:
            logger.warning(
                "generation_optimization.draft_action.upstream_unavailable",
                extra={
                    "draft_id": draft_id,
                    "action": action,
                    "upstream_status": upstream_status if isinstance(upstream_status, int) else None,
                    "error": error_msg,
                },
            )
            detail = {
                "error": {
                    "code": "upstream_unavailable",
                    "message": "视频生成上游暂时不可用,请稍后重试。",
                    "retryable": True,
                }
            }
            if isinstance(upstream_status, int):
                detail["error"]["upstream_status"] = upstream_status
            raise HTTPException(status_code=502, detail=detail)

        if "not found or expired" in error_msg:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": {
                        "code": "draft_not_found",
                        "message": f"Draft not found or expired: {draft_id}",
                    }
                },
            )
        if "cannot be confirmed" in error_msg or "cannot be rejected" in error_msg:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": {
                        "code": "draft_state_conflict",
                        "message": error_msg,
                    }
                },
            )
        if "has expired" in error_msg:
            raise HTTPException(
                status_code=410,
                detail={
                    "error": {
                        "code": "draft_expired",
                        "message": f"Draft has expired: {draft_id}",
                    }
                },
            )
        if "Regeneration limit reached" in error_msg:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": {
                        "code": "regeneration_limit_reached",
                        "message": error_msg,
                    }
                },
            )

        logger.error(
            "generation_optimization.draft_action.error",
            extra={
                "draft_id": draft_id,
                "action": action,
                "error": error_msg,
            },
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error": {
                    "code": "internal_error",
                    "message": "An error occurred while processing the draft action.",
                }
            },
        )


# ==================================================================
# 辅助函数
# ==================================================================


def _request_owner(request: Request) -> tuple[str | None, str | None]:
    api_key_data = getattr(request.state, "api_key_data", None)
    api_key_data = api_key_data if isinstance(api_key_data, dict) else {}
    user_id = str(
        getattr(request.state, "user_id", "")
        or api_key_data.get("user_id")
        or ""
    ).strip()
    group_id = str(
        getattr(request.state, "group_id", "")
        or api_key_data.get("group_id")
        or ""
    ).strip()
    return user_id or None, group_id or None


def _source_video_error(code: str) -> tuple[int, str]:
    errors: dict[str, tuple[int, str]] = {
        "source_draft_not_found": (404, "原图片草稿不存在、已过期或结果文件已清理。"),
        "source_draft_forbidden": (403, "无权使用该图片草稿。"),
        "source_draft_invalid_type": (409, "仅支持使用已完成的图片结果创建视频。"),
        "video_duration_unsupported": (422, "当前模型不支持该视频时长或帧率。"),
        "video_motion_prompt_missing": (422, "请描述主体动作或镜头运动。"),
        "comfyui_unavailable": (503, "本地视频生成服务当前不可用。"),
        "comfyui_video_not_enabled": (503, "本地视频生成能力未启用。"),
    }
    return errors.get(code, (500, "创建视频草稿时发生内部错误。"))


def _get_draft_strategy(request: Request) -> Any | None:
    """从 app state 获取 DraftGeneratorStrategy 实例."""
    strategy = getattr(request.app.state, "draft_generator_strategy", None)
    if strategy is not None:
        return strategy

    gen_opt = getattr(request.app.state, "generation_optimization", None)
    if gen_opt is not None:
        strategy = getattr(gen_opt, "draft_generator_strategy", None)
        if strategy is not None:
            return strategy

    return None
