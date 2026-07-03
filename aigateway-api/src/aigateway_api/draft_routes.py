"""
Draft Routes — Draft-to-HiRes 工作流 API 端点
==============================================

提供 /api/v1/generation/drafts/{draft_id}/action 端点，
接受用户对草图的 confirm 或 reject 操作，推进 Draft-to-HiRes 工作流。

需求: 3.6
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, field_validator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/generation", tags=["generation-optimization"])


# ==================================================================
# 请求/响应模型
# ==================================================================


class DraftActionRequest(BaseModel):
    """草图操作请求.

    Attributes:
        action: 操作类型，必须为 "confirm" 或 "reject"
    """

    action: str

    @field_validator("action")
    @classmethod
    def validate_action(cls, v: str) -> str:
        """验证 action 值必须为 confirm 或 reject."""
        if v not in ("confirm", "reject"):
            raise ValueError(
                f"Invalid action '{v}'. Must be 'confirm' or 'reject'."
            )
        return v


class DraftActionResponse(BaseModel):
    """草图操作响应.

    Attributes:
        draft_id: 草图标识符
        action: 执行的操作
        status: 操作结果状态
        data: 操作结果数据（confirm 时包含放大结果，reject 时包含新草图信息）
    """

    draft_id: str
    action: str
    status: str
    data: Dict[str, Any] = {}


# ==================================================================
# API 端点
# ==================================================================


@router.post("/drafts/{draft_id}/action", response_model=DraftActionResponse)
async def draft_action(
    draft_id: str,
    request_body: DraftActionRequest,
    request: Request,
) -> DraftActionResponse:
    """执行草图确认或拒绝操作.

    确认(confirm): 触发 Upscaler 放大到目标分辨率，返回放大结果。
    拒绝(reject): 删除当前草图并重新生成新的低分辨率草图。

    Args:
        draft_id: 草图唯一标识符
        request_body: 操作请求体，包含 action 字段
        request: FastAPI Request 对象，用于访问 app state

    Returns:
        DraftActionResponse 包含操作结果

    Raises:
        HTTPException 400: action 无效
        HTTPException 404: 草图不存在或已过期
        HTTPException 409: 草图状态不允许该操作（如已确认的草图不可再次确认）
        HTTPException 500: 内部处理错误
    """
    action = request_body.action

    # 获取 DraftGeneratorStrategy 实例 (from app state)
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

        elif action == "reject":
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

        else:
            # Should not reach here due to validator, but defensive
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

        # Map DraftWorkflowError messages to appropriate HTTP status codes
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
        elif "cannot be confirmed" in error_msg or "cannot be rejected" in error_msg:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": {
                        "code": "draft_state_conflict",
                        "message": error_msg,
                    }
                },
            )
        elif "has expired" in error_msg:
            raise HTTPException(
                status_code=410,
                detail={
                    "error": {
                        "code": "draft_expired",
                        "message": f"Draft has expired: {draft_id}",
                    }
                },
            )
        elif "Regeneration limit reached" in error_msg:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": {
                        "code": "regeneration_limit_reached",
                        "message": error_msg,
                    }
                },
            )
        else:
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


def _get_draft_strategy(request: Request) -> Optional[Any]:
    """从 app state 获取 DraftGeneratorStrategy 实例.

    Args:
        request: FastAPI Request 对象

    Returns:
        DraftGeneratorStrategy 实例，不存在时返回 None
    """
    # 尝试从 app.state.draft_generator_strategy 获取
    strategy = getattr(request.app.state, "draft_generator_strategy", None)
    if strategy is not None:
        return strategy

    # 尝试从 generation_optimization 命名空间获取
    gen_opt = getattr(request.app.state, "generation_optimization", None)
    if gen_opt is not None:
        strategy = getattr(gen_opt, "draft_generator_strategy", None)
        if strategy is not None:
            return strategy

    return None
