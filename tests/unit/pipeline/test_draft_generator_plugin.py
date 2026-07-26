"""
Tests for DraftGeneratorPlugin._build_generation_request media_type propagation.

Regression: ctx.pipeline_kind='generation:video' must produce GenerationRequest
with media_type='video', so confirm_draft hits the video branch (not image upscale).
"""

import sys
from pathlib import Path

# Ensure aigateway-core src on path (matches conftest pattern)
_CORE = Path(__file__).resolve().parents[2] / "aigateway-core" / "src"  # tests/unit/pipeline/ → tests/ (parents[2])
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from aigateway_core.dispatch.context import PipelineContext
from aigateway_core.pipelines.generation._common.config import (
    GenerationOptimizationConfig,
)
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
