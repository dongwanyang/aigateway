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
            content={"error": {"code": "bridge_unavailable", "message": "LiteLLM bridge not initialized"}},
            status_code=503,
        )
    try:
        result: Dict[str, Any] = await bridge.retrieve_video(video_id)
        return JSONResponse(content=result)
    except Exception as exc:
        # 生产环境不暴露 provider 内部错误细节，仅 debug 模式下透传。
        from aigateway_core.shared.debug_config import DebugConfig
        from aigateway_core.shared.trace_event import TraceCollector
        debug_detail = False
        try:
            collector = TraceCollector.current()
            if collector is not None:
                debug_detail = collector.get_debug_dimension("bridge") is True
        except Exception:
            pass
        message = str(exc) if debug_detail else "Video retrieval failed"
        return JSONResponse(
            content={"error": {"code": "video_retrieve_failed", "message": message}},
            status_code=502,
        )
