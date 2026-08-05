"""Security-first replacement for draft confirmation with stable error codes."""
from __future__ import annotations

import base64
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from aigateway_core.pipelines.generation._common.models import VideoSubmitResult

from .auth_middleware import authenticate_admin
from .draft_security import assert_draft_owner

logger = logging.getLogger(__name__)
router = APIRouter()


def _strategy(request: Request) -> Any:
    strategy = getattr(request.app.state, "draft_strategy", None)
    if strategy is None:
        strategy = getattr(request.app.state, "draft_generator_strategy", None)
    if strategy is None:
        from .app_state import get_state

        state = get_state()
        strategy = getattr(state, "draft_strategy", None) or getattr(
            state, "draft_generator_strategy", None
        )
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


def _error_code(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.split(":", 1)[0].strip() or "draft_confirm_failed"


def _confirm_error(exc: BaseException, draft_id: str) -> HTTPException:
    text = str(exc)
    code = _error_code(exc)
    exact: dict[str, tuple[int, str, bool]] = {
        "video_keyframe_integrity_mismatch": (
            409,
            "关键帧已变化，请重新创建视频草稿。",
            False,
        ),
        "video_keyframe_integrity_missing": (
            409,
            "视频草稿缺少冻结的关键帧标识，请重新创建草稿。",
            False,
        ),
        "video_duration_unsupported": (
            422,
            "当前模型不支持该视频时长。",
            False,
        ),
        "video_fps_invalid": (422, "当前模型不支持该视频帧率。", False),
        "comfyui_video_workflow_invalid": (
            503,
            "视频工作流配置无效，请联系管理员检查部署版本。",
            False,
        ),
        "comfyui_video_not_enabled": (
            503,
            "本地视频生成能力未启用。",
            False,
        ),
        "comfyui_unavailable": (
            503,
            "本地视频生成服务当前不可用。",
            True,
        ),
        "comfyui_gpu_out_of_memory": (
            503,
            "GPU 显存不足，请降低生成时长或稍后重试。",
            True,
        ),
    }
    if code in exact:
        status, message, retryable = exact[code]
        return HTTPException(
            status_code=status,
            detail={
                "error": {
                    "code": code,
                    "message": message,
                    "retryable": retryable,
                }
            },
        )

    upstream_status = getattr(exc, "upstream_status", None)
    if (
        (isinstance(upstream_status, int) and upstream_status >= 500)
        or getattr(exc, "upstream_unavailable", False)
    ):
        detail: dict[str, Any] = {
            "error": {
                "code": "upstream_unavailable",
                "message": "视频生成上游暂时不可用，请稍后重试。",
                "retryable": True,
            }
        }
        if isinstance(upstream_status, int):
            detail["error"]["upstream_status"] = upstream_status
        return HTTPException(status_code=502, detail=detail)
    if "not found or expired" in text or code == "draft_not_found":
        return HTTPException(
            status_code=404,
            detail={
                "error": {
                    "code": "draft_not_found",
                    "message": f"Draft not found or expired: {draft_id}",
                }
            },
        )
    if "has expired" in text or code == "draft_expired":
        return HTTPException(
            status_code=410,
            detail={
                "error": {
                    "code": "draft_expired",
                    "message": f"Draft has expired: {draft_id}",
                }
            },
        )
    if "cannot be confirmed" in text or code == "draft_state_conflict":
        return HTTPException(
            status_code=409,
            detail={
                "error": {
                    "code": "draft_state_conflict",
                    "message": "草稿当前状态不可确认，请刷新后重试。",
                }
            },
        )
    if code.startswith("comfyui_storage_") or "budget_exceeded" in text:
        return HTTPException(
            status_code=507,
            detail={
                "error": {
                    "code": "comfyui_storage_unavailable",
                    "message": "本地生成存储空间不足。",
                    "retryable": False,
                }
            },
        )
    return HTTPException(
        status_code=500,
        detail={
            "error": {
                "code": "draft_confirm_failed",
                "message": "草稿确认失败，请查看服务端日志中的 trace_id。",
            }
        },
    )


async def _record_confirmation_request(
    request: Request,
    *,
    draft_id: str,
    duration_ms: float,
    model: str,
) -> None:
    """Preserve the existing admin Logs-page audit record without blocking output."""
    try:
        from .openai_compat import _record_request_log

        await _record_request_log(
            request=request,
            method="POST",
            endpoint=f"/admin/draft/{draft_id}/confirm",
            status_code=200,
            duration_ms=duration_ms,
            model=model,
            cache_hit=False,
            cache_tier=None,
        )
    except Exception as exc:
        logger.warning(
            "generation_optimization.draft_confirm.audit_log_failed",
            extra={"draft_id": draft_id, "error_type": type(exc).__name__},
        )


@router.post("/draft/{draft_id}/confirm")
async def confirm_draft(
    draft_id: str,
    request: Request,
    auth: dict[str, Any] = Depends(authenticate_admin),
) -> dict[str, Any]:
    """Confirm an owned draft and preserve machine-readable failure semantics."""
    strategy = _strategy(request)
    draft = await strategy.get_draft(draft_id)
    if draft is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "code": "draft_not_found",
                    "message": f"Draft '{draft_id}' not found",
                }
            },
        )
    assert_draft_owner(draft, auth, action="confirm it")

    try:
        result = await strategy.confirm_draft(draft_id)
    except HTTPException:
        raise
    except Exception as exc:
        mapped = _confirm_error(exc, draft_id)
        context = {
            "draft_id": draft_id,
            "media_type": getattr(draft, "media_type", "image"),
            "error_code": _error_code(exc),
            "trace_id": str(
                getattr(draft, "generation_params", {}).get("trace_id") or ""
            ),
        }
        if mapped.status_code < 500:
            logger.warning("generation_optimization.draft_confirm.failed", extra=context)
        else:
            logger.exception("generation_optimization.draft_confirm.failed", extra=context)
        raise mapped from exc

    if isinstance(result, VideoSubmitResult):
        await _record_confirmation_request(
            request,
            draft_id=draft_id,
            duration_ms=0.0,
            model="agnes-video-v2.0",
        )
        return {
            "draft_id": draft_id,
            "video_id": result.video_id,
            "status": result.status,
            "media_type": "video",
        }

    output_data = result.output_data
    if isinstance(output_data, bytes):
        from .admin_routes import _detect_media_mime

        encoded = base64.b64encode(output_data).decode("ascii")
        output_url = f"data:{_detect_media_mime(output_data)};base64,{encoded}"
    else:
        output_url = str(output_data)[:500]
    await _record_confirmation_request(
        request,
        draft_id=draft_id,
        duration_ms=float(result.duration_ms or 0),
        model=result.algorithm_used or "comfyui",
    )
    return {
        "draft_id": draft_id,
        "upscaled_url": output_url,
        "target_resolution": list(result.target_resolution),
        "algorithm": result.algorithm_used,
        "duration_ms": result.duration_ms,
        "media_type": getattr(draft, "media_type", "image"),
    }


def _route_conflicts(existing: Any, replacements: list[Any]) -> bool:
    path = getattr(existing, "path", None)
    methods = set(getattr(existing, "methods", set()) or set())
    return any(
        getattr(replacement, "path", None) == path
        and bool(methods & set(getattr(replacement, "methods", set()) or set()))
        for replacement in replacements
    )


def install_draft_confirm_routes(admin_router: APIRouter) -> None:
    marker = "_aigateway_draft_confirm_routes_installed"
    if getattr(admin_router, marker, False):
        return
    replacements = list(router.routes)
    admin_router.routes[:] = [
        route for route in admin_router.routes if not _route_conflicts(route, replacements)
    ]
    admin_router.routes[0:0] = replacements
    setattr(admin_router, marker, True)


__all__ = ["_confirm_error", "install_draft_confirm_routes", "router"]
