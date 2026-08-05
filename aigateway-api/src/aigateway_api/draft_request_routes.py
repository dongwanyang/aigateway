"""Recover and cancel progressive generation requests by stable request ID."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError

from .auth_middleware import authenticate_admin
from .draft_security import assert_draft_owner

router = APIRouter()


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


def _owner_value(auth: dict[str, Any], key: str) -> str | None:
    value = str(auth.get(key) or "").strip()
    return value or None


def _assert_request_record_owner(
    record: dict[str, Any],
    auth: dict[str, Any],
    chat_session_id: str,
) -> None:
    expected_user = str(record.get("user_id") or "")
    expected_group = str(record.get("group_id") or "")
    expected_session = str(record.get("session_id") or "")
    actual_user = str(auth.get("user_id") or "")
    actual_group = str(auth.get("group_id") or "")
    if (
        (expected_user and expected_user != actual_user)
        or (expected_group and expected_group != actual_group)
        or (expected_session and expected_session != chat_session_id)
        or (not expected_user and not expected_group)
    ):
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "code": "generation_request_forbidden",
                    "message": "无权访问该生成请求。",
                }
            },
        )


def _resolving_response(request_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "request_id": request_id,
            "status": "resolving",
            "retry_after_ms": 250,
        },
    )


def _draft_payload(request_id: str, draft: Any) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "draft_id": draft.draft_id,
        "status": draft.status,
        "stage": draft.stage,
        "progress": draft.progress,
        "media_type": draft.media_type,
        "preview_url": f"/admin/draft/{draft.draft_id}/preview",
        "expires_at": draft.expires_at,
        "workflow_version": draft.workflow_version,
        "error": draft.error,
    }


@router.get("/generation/requests/{request_id}")
async def get_generation_request(
    request_id: str,
    request: Request,
    chat_session_id: str = Query(min_length=1, max_length=128),
    auth: dict[str, Any] = Depends(authenticate_admin),
) -> Any:
    strategy = _strategy(request)
    draft, record = await strategy.resolve_request(request_id)
    if record is None:
        return _resolving_response(request_id)
    _assert_request_record_owner(record, auth, chat_session_id)
    if draft is None and not str(record.get("draft_id") or ""):
        return _resolving_response(request_id)
    if draft is None:
        raise HTTPException(
            status_code=410,
            detail={
                "error": {
                    "code": "generation_request_expired",
                    "message": "生成请求已过期或草稿已清理。",
                }
            },
        )
    assert_draft_owner(draft, auth, action="access this generation request")
    if str(draft.session_id or "") != chat_session_id:
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "code": "generation_request_forbidden",
                    "message": "生成请求不属于当前会话。",
                }
            },
        )
    return _draft_payload(request_id, draft)


@router.delete("/generation/requests/{request_id}")
async def cancel_generation_request(
    request_id: str,
    request: Request,
    chat_session_id: str = Query(min_length=1, max_length=128),
    auth: dict[str, Any] = Depends(authenticate_admin),
) -> Any:
    strategy = _strategy(request)
    try:
        draft = await strategy.cancel_request(
            request_id,
            user_id=_owner_value(auth, "user_id"),
            group_id=_owner_value(auth, "group_id"),
            session_id=chat_session_id,
        )
    except DraftWorkflowError as exc:
        code = str(exc).split(":", 1)[0]
        if code == "generation_request_forbidden":
            raise HTTPException(
                status_code=403,
                detail={
                    "error": {
                        "code": code,
                        "message": "无权取消该生成请求。",
                    }
                },
            ) from exc
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "code": "generation_request_not_found",
                    "message": "生成请求不存在或已过期。",
                }
            },
        ) from exc

    if draft is None:
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "request_id": request_id,
                "status": "cancellation_requested",
            },
        )
    assert_draft_owner(draft, auth, action="cancel this generation request")
    return _draft_payload(request_id, draft)


def install_draft_request_routes(admin_router: APIRouter) -> None:
    marker = "_aigateway_draft_request_routes_installed"
    if getattr(admin_router, marker, False):
        return
    admin_router.include_router(router)
    setattr(admin_router, marker, True)


__all__ = ["install_draft_request_routes", "router"]
