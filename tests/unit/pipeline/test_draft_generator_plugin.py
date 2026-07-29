"""
Tests for DraftGeneratorPlugin._build_generation_request media_type propagation.

Regression: ctx.pipeline_kind='generation:video' must produce GenerationRequest
with media_type='video', so confirm_draft hits the video branch (not image upscale).
"""

import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

# Ensure aigateway-core src on path (matches conftest pattern)
_CORE = Path(__file__).resolve().parents[2] / "aigateway-core" / "src"  # tests/unit/pipeline/ → tests/ (parents[2])
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from aigateway_core.dispatch.context import PipelineContext
from aigateway_core.pipelines.generation._common.config import (
    GenerationOptimizationConfig,
)
from aigateway_core.pipelines.generation._common.models import DraftResult
from aigateway_core.pipelines.generation.draft.draft_generator_plugin import (
    DraftGeneratorPlugin,
)


def _make_plugin() -> DraftGeneratorPlugin:
    config = GenerationOptimizationConfig()
    # _build_generation_request doesn't touch strategy; None is fine.
    return DraftGeneratorPlugin(strategy=None, config=config)


def _ctx(pipeline_kind: str, prompt: str = "sunset over ocean") -> PipelineContext:
    return PipelineContext(
        request={"messages": [{"role": "user", "content": prompt}]},
        trace_id="trace-test",
        pipeline_kind=pipeline_kind,
    )


def test_build_request_video_kind_sets_media_type_video():
    """ctx.pipeline_kind='generation:video' → GenerationRequest.media_type=='video'."""
    plugin = _make_plugin()
    ctx = _ctx("generation:video")
    req = plugin._build_generation_request(ctx)
    assert req.media_type == "video"


def test_build_request_image_kind_sets_media_type_image():
    """ctx.pipeline_kind='generation:image' → GenerationRequest.media_type=='image'."""
    plugin = _make_plugin()
    ctx = _ctx("generation:image")
    req = plugin._build_generation_request(ctx)
    assert req.media_type == "image"


def test_build_request_understanding_kind_defaults_image():
    """Non-generation pipeline_kind falls back to image (default)."""
    plugin = _make_plugin()
    ctx = _ctx("understanding")
    req = plugin._build_generation_request(ctx)
    assert req.media_type == "image"


@pytest.mark.asyncio
async def test_execute_uses_explicit_browser_draft_owner_without_billing_group():
    config = GenerationOptimizationConfig()
    strategy = AsyncMock()
    now = time.time()
    strategy.generate_draft.return_value = DraftResult(
        draft_id="draft-browser-owner",
        previews=[],
        generation_params={},
        created_at=now,
        expires_at=now + 60,
        status="generating",
    )
    strategy.checkpoint_name = "test-checkpoint"
    plugin = DraftGeneratorPlugin(strategy=strategy, config=config)
    ctx = _ctx("generation:image")
    ctx.user_id = "billing-user"
    ctx.extra["group_id"] = "grp-default"
    ctx.extra["draft_owner_user_id"] = "browser-admin-id"
    ctx.extra["draft_owner_group_id"] = None
    ctx.extra["chat_session_id"] = "sess-browser"

    await plugin.execute(ctx)

    assert strategy.generate_draft.await_args.kwargs["user_id"] == "browser-admin-id"
    assert strategy.generate_draft.await_args.kwargs["group_id"] is None
    assert strategy.generate_draft.await_args.kwargs["chat_session_id"] == "sess-browser"
