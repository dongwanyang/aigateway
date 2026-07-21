"""Video 轮询 endpoint —— GET /v1/videos/{id}.

对应 OpenAI Videos API 的 Retrieve a video, 供客户端轮询视频生成任务状态。
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .app_state import get_state
from .auth_middleware import authenticate

router = APIRouter()


@router.get("/videos/{video_id}")
async def retrieve_video(
    video_id: str,
    request: Request,
    _auth: Dict[str, Any] = Depends(authenticate),
) -> JSONResponse:
    """轮询视频任务状态."""
    state = get_state()
    bridge = getattr(state, "litellm_bridge", None)
    if bridge is None:
        return JSONResponse(
            content={"error": {"code": "internal_error", "message": "LiteLLM bridge not initialized"}},
            status_code=500,
        )
    try:
        result: Dict[str, Any] = await bridge.retrieve_video(video_id)
        return JSONResponse(content=result)
    except Exception as exc:
        return JSONResponse(
            content={"error": {"code": "video_retrieve_failed", "message": str(exc)}},
            status_code=502,
        )
