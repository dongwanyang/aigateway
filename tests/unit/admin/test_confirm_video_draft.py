"""Coverage for DraftGeneratorStrategy._confirm_video_draft error paths.

The video confirm branch (added on feature/intent-driven-routing) has several
failure modes that the happy-path test in test_draft_generator_strategy.py
does not exercise:
- bridge not bound → DraftWorkflowError
- _resolve_by_intent returns error → DraftWorkflowError
- bridge._do_video_generation raises generic Exception → wrapped DraftWorkflowError
- Agnes returns no video_id → DraftWorkflowError
- model hint already in generation_params → skips _resolve_by_intent
"""

import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.pipelines.generation._common.config import DraftWorkflowConfig
from aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError
from aigateway_core.pipelines.generation._common.models import (
    DRAFT_STATUS_GENERATING,
    GenerationRequest,
    VideoSubmitResult,
)
from aigateway_core.pipelines.generation.draft.draft_generator import (
    DraftGeneratorStrategy,
)


@pytest.fixture
def default_config(tmp_path):
    return DraftWorkflowConfig(
        enabled=True,
        draft_resolution=(512, 512),
        default_target_resolution=(1920, 1080),
        max_target_resolution=(4096, 4096),
        max_regeneration_attempts=5,
        retention_period_hours=24,
        preview_video_duration_seconds=30,
        preview_keyframe_interval_seconds=5,
        preview_video_fps=8,
        target_fps=60,
        target_fps_range=(24, 120),
        upscale_algorithm="real-esrgan",
        store_dir=str(tmp_path / "drafts"),
    )


@pytest.fixture
def strategy(default_config, monkeypatch):
    instance = DraftGeneratorStrategy(config=default_config, redis_client=None)
    # A unit test must not probe a developer's ComfyUI service.  Make the
    # production fallback path explicit and deterministic, while still running
    # the real background draft-generation state transition.
    monkeypatch.setattr(instance, "_check_comfyui", AsyncMock(return_value=None))
    return instance


@pytest.fixture
def video_request():
    return GenerationRequest(
        prompt="A video of a cat playing with a ball",
        target_resolution=(1920, 1080),
        target_fps=60,
        media_type="video",
    )


async def _await_generating(strategy, draft_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = await strategy.get_draft(draft_id)
        if d is not None and d.status != DRAFT_STATUS_GENERATING:
            return d
        await asyncio.sleep(0.01)
    pytest.fail(
        f"draft {draft_id} did not leave generating within {timeout}s; "
        f"background_tasks={len(strategy._bg_tasks)}"
    )


@pytest.mark.asyncio
async def test_confirm_video_no_bridge_raises(strategy, video_request, default_config):
    """Video confirm with no LiteLLM bridge bound → DraftWorkflowError mentioning bridge."""
    result = await strategy.generate_draft(video_request, default_config)
    draft = await _await_generating(strategy, result.draft_id)
    assert strategy._litellm_bridge is None

    with pytest.raises(DraftWorkflowError, match="bridge not bound"):
        await strategy.confirm_draft(draft.draft_id)


@pytest.mark.asyncio
async def test_confirm_video_resolve_error_raises(
    strategy, video_request, default_config
):
    """_resolve_by_intent returns {'error': ...} → DraftWorkflowError mentioning resolution."""
    from unittest.mock import AsyncMock

    result = await strategy.generate_draft(video_request, default_config)
    draft = await _await_generating(strategy, result.draft_id)
    # Clear any model hint so _confirm_video_draft calls _resolve_by_intent
    draft.generation_params.pop("model", None)
    await strategy._store_draft(draft, ttl_seconds=60)

    bridge = AsyncMock()
    bridge._resolve_by_intent = AsyncMock(return_value={
        "error": {"code": "no_model_for_intent", "message": "no video model"}
    })
    bridge._do_video_generation = AsyncMock()
    strategy._litellm_bridge = bridge

    with pytest.raises(DraftWorkflowError, match="video model resolution failed"):
        await strategy.confirm_draft(draft.draft_id)
    bridge._do_video_generation.assert_not_called()


@pytest.mark.asyncio
async def test_confirm_video_bridge_exception_wrapped(
    strategy, video_request, default_config
):
    """Generic exception from _do_video_generation → wrapped in DraftWorkflowError."""
    from unittest.mock import AsyncMock

    result = await strategy.generate_draft(video_request, default_config)
    draft = await _await_generating(strategy, result.draft_id)
    draft.generation_params.pop("model", None)
    await strategy._store_draft(draft, ttl_seconds=60)

    bridge = AsyncMock()
    bridge._resolve_by_intent = AsyncMock(return_value={
        "model": "agnes-video-v2.0",
        "meta": {"reason": "pool_first"},
    })
    bridge._do_video_generation = AsyncMock(side_effect=RuntimeError("agnes 500"))
    strategy._litellm_bridge = bridge

    with pytest.raises(DraftWorkflowError, match="Agnes /videos submission failed"):
        await strategy.confirm_draft(draft.draft_id)


@pytest.mark.asyncio
async def test_confirm_video_upstream_503_tagged_retryable(
    strategy, video_request, default_config
):
    """上游 Agnes /videos 返回 503 → DraftWorkflowError 带 upstream_status=503 + upstream_unavailable 前缀。

    confirm 路由据此返回 502 retryable 而非 400。用真实 httpx.HTTPStatusError 模拟,
    验证 _upstream_http_status 能从异常链取出 response.status_code。
    """
    import httpx
    from unittest.mock import AsyncMock

    result = await strategy.generate_draft(video_request, default_config)
    draft = await _await_generating(strategy, result.draft_id)
    draft.generation_params.pop("model", None)
    await strategy._store_draft(draft, ttl_seconds=60)

    bridge = AsyncMock()
    bridge._resolve_by_intent = AsyncMock(return_value={
        "model": "agnes-video-v2.0",
        "meta": {"reason": "pool_first"},
    })
    # 模拟 Agnes /videos 返回 503 —— _do_video_generation 内部 resp.raise_for_status() 抛的就是这个
    req = httpx.Request("POST", "https://apihub.agnes-ai.com/v1/videos")
    resp_503 = httpx.Response(status_code=503, request=req)
    bridge._do_video_generation = AsyncMock(
        side_effect=httpx.HTTPStatusError("503 Server Unavailable", request=req, response=resp_503)
    )
    strategy._litellm_bridge = bridge

    with pytest.raises(DraftWorkflowError) as exc_info:
        await strategy.confirm_draft(draft.draft_id)

    err = exc_info.value
    assert err.upstream_status == 503
    assert err.upstream_unavailable is True
    assert "upstream_unavailable" in str(err)


@pytest.mark.asyncio
async def test_confirm_video_network_error_tagged_retryable(
    strategy, video_request, default_config
):
    """上游 Agnes 完全连不上(ConnectError,无 HTTP 响应)→ DraftWorkflowError 带
    upstream_unavailable=True 但无 upstream_status。

    confirm 路由据此仍返回 502 retryable 而非 400。验证 _is_upstream_network_error
    能从异常链识别 httpx.TransportError/TimeoutException。
    """
    import httpx
    from unittest.mock import AsyncMock

    result = await strategy.generate_draft(video_request, default_config)
    draft = await _await_generating(strategy, result.draft_id)
    draft.generation_params.pop("model", None)
    await strategy._store_draft(draft, ttl_seconds=60)

    bridge = AsyncMock()
    bridge._resolve_by_intent = AsyncMock(return_value={
        "model": "agnes-video-v2.0",
        "meta": {"reason": "pool_first"},
    })
    # 模拟 Agnes 完全不可达 —— _do_video_generation 内部 client.post 抛 ConnectError
    req = httpx.Request("POST", "https://apihub.agnes-ai.com/v1/videos")
    bridge._do_video_generation = AsyncMock(
        side_effect=httpx.ConnectError("[Errno 111] Connection refused", request=req)
    )
    strategy._litellm_bridge = bridge

    with pytest.raises(DraftWorkflowError) as exc_info:
        await strategy.confirm_draft(draft.draft_id)

    err = exc_info.value
    # 网络故障:无 HTTP 状态码,但 upstream_unavailable 标记必须为 True
    assert err.upstream_unavailable is True
    assert not hasattr(err, "upstream_status")
    assert "upstream_unavailable" in str(err)
    assert "network error" in str(err)


@pytest.mark.asyncio
async def test_confirm_video_no_video_id_raises(
    strategy, video_request, default_config
):
    """Agnes returns no video_id in _meta → DraftWorkflowError."""
    from unittest.mock import AsyncMock

    result = await strategy.generate_draft(video_request, default_config)
    draft = await _await_generating(strategy, result.draft_id)
    draft.generation_params.pop("model", None)
    await strategy._store_draft(draft, ttl_seconds=60)

    bridge = AsyncMock()
    bridge._resolve_by_intent = AsyncMock(return_value={
        "model": "agnes-video-v2.0",
        "meta": {"reason": "pool_first"},
    })
    # _meta missing or video_id empty
    bridge._do_video_generation = AsyncMock(return_value={
        "_meta": {},
        "usage": {},
    })
    strategy._litellm_bridge = bridge

    with pytest.raises(DraftWorkflowError, match="no video_id"):
        await strategy.confirm_draft(draft.draft_id)


@pytest.mark.asyncio
async def test_confirm_video_model_hint_skips_resolve(
    strategy, video_request, default_config
):
    """If generation_params has 'model', _resolve_by_intent is NOT called."""
    from unittest.mock import AsyncMock

    result = await strategy.generate_draft(video_request, default_config)
    draft = await _await_generating(strategy, result.draft_id)
    draft.generation_params["model"] = "agnes-video-v2.0"
    await strategy._store_draft(draft, ttl_seconds=60)

    bridge = AsyncMock()
    bridge._do_video_generation = AsyncMock(return_value={
        "_meta": {"video_id": "vid_hint_ok"},
        "usage": {},
    })
    strategy._litellm_bridge = bridge

    out = await strategy.confirm_draft(draft.draft_id)
    assert isinstance(out, VideoSubmitResult)
    assert out.video_id == "vid_hint_ok"
    bridge._resolve_by_intent.assert_not_called()
    # _do_video_generation called with the hinted model
    call_kwargs = bridge._do_video_generation.call_args.kwargs
    assert call_kwargs["model"] == "agnes-video-v2.0"


@pytest.mark.asyncio
async def test_confirm_video_passes_prompt_as_messages(
    strategy, video_request, default_config
):
    """_confirm_video_draft builds messages=[{role:user, content:<prompt>}] from draft."""
    from unittest.mock import AsyncMock

    result = await strategy.generate_draft(video_request, default_config)
    draft = await _await_generating(strategy, result.draft_id)
    draft.generation_params["model"] = "agnes-video-v2.0"
    await strategy._store_draft(draft, ttl_seconds=60)

    bridge = AsyncMock()
    bridge._do_video_generation = AsyncMock(return_value={
        "_meta": {"video_id": "vid_msg"},
        "usage": {},
    })
    strategy._litellm_bridge = bridge

    await strategy.confirm_draft(draft.draft_id)
    call_kwargs = bridge._do_video_generation.call_args.kwargs
    msgs = call_kwargs["messages"]
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    assert "cat playing with a ball" in msgs[0]["content"]
