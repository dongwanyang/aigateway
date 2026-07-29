from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aigateway_core.pipelines.generation._common.config import DraftWorkflowConfig
from aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError
from aigateway_core.pipelines.generation._common.models import GenerationRequest
from aigateway_core.pipelines.generation.draft import draft_generator as module
from aigateway_core.pipelines.generation.draft.draft_generator import (
    DraftGeneratorStrategy,
)
from aigateway_core.shared.integration_configs import ComfyUIConfig


@pytest.fixture
def strategy(tmp_path):
    comfy = ComfyUIConfig(
        checkpoint_name="approved.safetensors",
        allowed_checkpoints=["approved.safetensors"],
        models_path=str(tmp_path / "models"),
        output_path=str(tmp_path / "output"),
        min_free_gb=1,
        model_budget_gb=30,
        output_budget_gb=10,
        video_enabled=True,
    )
    return DraftGeneratorStrategy(
        DraftWorkflowConfig(
            draft_resolution=(512, 512),
            store_dir=str(tmp_path / "drafts"),
        ),
        comfyui_config=comfy,
        store_dir=str(tmp_path / "drafts"),
    )


def test_draft_workflow_uses_allowlisted_checkpoint_and_stable_seed(strategy):
    request = GenerationRequest(prompt="ocean sunset", request_id="req-1")

    workflow = strategy._build_image_draft_workflow(
        request, seed=123456
    )

    assert workflow["3"]["inputs"]["seed"] == 123456
    assert workflow["3"]["inputs"]["steps"] == 12
    assert workflow["4"]["inputs"]["ckpt_name"] == "approved.safetensors"


def test_refine_workflow_reuses_prompt_seed_checkpoint_and_preview(strategy):
    workflow = strategy._build_refine_workflow(
        input_name="draft-1.png",
        prompt="ocean sunset",
        seed=123456,
        target_resolution=(1920, 1080),
    )

    assert workflow["1"]["inputs"]["image"] == "draft-1.png"
    assert workflow["2"]["inputs"]["width"] == 1920
    assert workflow["2"]["inputs"]["height"] == 1080
    assert workflow["3"]["inputs"]["ckpt_name"] == "approved.safetensors"
    assert workflow["5"]["inputs"]["text"] == "ocean sunset"
    assert workflow["7"]["inputs"]["seed"] == 123456
    assert workflow["7"]["inputs"]["denoise"] == 0.25


@pytest.mark.asyncio
async def test_comfyui_unavailable_fails_closed_without_provider_fallback(
    strategy, monkeypatch
):
    monkeypatch.setattr(
        strategy,
        "_check_comfyui",
        AsyncMock(side_effect=DraftWorkflowError("ComfyUI service is unavailable")),
    )
    strategy._litellm_bridge = AsyncMock()

    with pytest.raises(DraftWorkflowError, match="unavailable"):
        await strategy._generate_image_preview_with_comfyui(
            GenerationRequest(prompt="test"),
            strategy._config,
            seed=1,
        )

    strategy._litellm_bridge._do_image_generation.assert_not_called()


@pytest.mark.asyncio
async def test_storage_low_fails_before_workflow_submission(strategy, monkeypatch):
    async def run_inline(func, *args):
        return func(*args)

    monkeypatch.setattr(module.asyncio, "to_thread", run_inline)
    strategy._comfyui_config.min_free_gb = 1_000_000

    with pytest.raises(DraftWorkflowError, match="comfyui_storage_low"):
        await strategy._ensure_storage_capacity()


def test_checkpoint_outside_allowlist_is_rejected(strategy):
    strategy._comfyui_config.checkpoint_name = "unapproved.safetensors"

    with pytest.raises(DraftWorkflowError, match="not allowlisted"):
        strategy._build_image_draft_workflow(
            GenerationRequest(prompt="test"), seed=1
        )


def test_wan_video_workflow_reuses_approved_keyframe_prompt_and_seed(strategy):
    workflow = strategy._build_video_workflow(
        input_name="video-keyframe-draft-1.png",
        prompt="a paper boat moving across a lake",
        seed=123456,
        draft_id="draft-1",
    )

    assert workflow["1"]["inputs"]["unet_name"] == (
        "wan2.2_ti2v_5B_fp16.safetensors"
    )
    assert workflow["2"]["inputs"]["type"] == "wan"
    assert workflow["4"]["inputs"]["image"] == "video-keyframe-draft-1.png"
    assert workflow["5"]["inputs"]["start_image"] == ["4", 0]
    assert workflow["6"]["inputs"]["text"] == (
        "a paper boat moving across a lake"
    )
    assert workflow["9"]["inputs"]["seed"] == 123456
    assert workflow["12"]["class_type"] == "SaveVideo"
    assert workflow["12"]["inputs"]["format"] == "mp4"


@pytest.mark.asyncio
async def test_video_preview_is_one_sdxl_keyframe(strategy, monkeypatch):
    generate_image = AsyncMock(return_value=b"approved-keyframe")
    monkeypatch.setattr(
        strategy, "_generate_image_preview_with_comfyui", generate_image
    )
    request = GenerationRequest(prompt="moving clouds", request_id="req-video")

    result = await strategy._generate_video_previews_with_comfyui(
        request,
        strategy._config,
        seed=17,
        draft_id="draft-video",
    )

    assert result == [b"approved-keyframe"]
    generate_image.assert_awaited_once_with(
        request,
        strategy._config,
        seed=17,
        draft_id="draft-video",
    )


@pytest.mark.asyncio
async def test_poll_results_downloads_comfyui_mp4_output(strategy, monkeypatch):
    class Response:
        def __init__(self, payload=None, content=b""):
            self.status_code = 200
            self._payload = payload
            self.content = content

        def json(self):
            return self._payload

    history = {
        "prompt-video": {
            "status": {"messages": []},
            "outputs": {
                "12": {
                    "videos": [
                        {
                            "filename": "video_draft.mp4",
                            "subfolder": "",
                            "type": "output",
                        }
                    ]
                }
            },
        }
    }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url):
            if "/history/" in url:
                return Response(history)
            return Response(content=b"\x00\x00\x00\x18ftypmp42video")

    monkeypatch.setattr("httpx.AsyncClient", lambda **_kwargs: Client())

    result = await strategy._poll_result("prompt-video", timeout=1)

    assert result.startswith(b"\x00\x00\x00\x18ftyp")


@pytest.mark.asyncio
async def test_comfyui_oom_is_reported_as_retryable_workflow_error(
    strategy, monkeypatch
):
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "prompt-1": {
                    "status": {
                        "messages": [
                            [
                                "execution_error",
                                {"exception_message": "CUDA out of memory"},
                            ]
                        ]
                    },
                    "outputs": {},
                }
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, _url):
            return Response()

    monkeypatch.setattr("httpx.AsyncClient", lambda **_kwargs: Client())

    with pytest.raises(DraftWorkflowError, match="gpu_out_of_memory"):
        await strategy._poll_results("prompt-1", timeout=1)
