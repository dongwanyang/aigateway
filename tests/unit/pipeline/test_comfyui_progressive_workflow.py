from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from aigateway_core.pipelines.generation._common.config import DraftWorkflowConfig
from aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError
from aigateway_core.pipelines.generation._common.models import (
    DRAFT_STATUS_RUNNING,
    DraftResult,
    GenerationRequest,
)
from aigateway_core.pipelines.generation.draft import _draft_generator_impl as impl
from aigateway_core.pipelines.generation.draft import draft_generator as module
from aigateway_core.pipelines.generation.draft.draft_generator import (
    DraftGeneratorStrategy,
)
from aigateway_core.prefix.media.types import MediaContent, MediaType
from aigateway_core.shared.integration_configs import ComfyUIConfig
from PIL import Image


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


class FakeRedis:
    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}
        self.ttls: dict[str, int] = {}
        self.values: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, ex=None):
        self.values[key] = value
        if ex is not None:
            self.ttls[key] = ex

    async def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    async def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    async def expire(self, key, ttl):
        self.ttls[key] = ttl

    async def sadd(self, key, value):
        self.sets.setdefault(key, set()).add(value)


def reference_png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (24, 16), color=(180, 120, 40)).save(output, "PNG")
    return output.getvalue()


def test_draft_workflow_uses_allowlisted_checkpoint_and_stable_seed(strategy):
    request = GenerationRequest(prompt="ocean sunset", request_id="req-1")

    workflow = strategy._build_image_draft_workflow(
        request, seed=123456
    )

    assert workflow["3"]["inputs"]["seed"] == 123456
    assert workflow["3"]["inputs"]["steps"] == 12
    assert workflow["4"]["inputs"]["ckpt_name"] == "approved.safetensors"


def test_qwen_draft_caps_t4_resolution_and_uses_configured_steps(
    strategy, monkeypatch
):
    strategy._comfyui_config.qwen_image_draft_steps = 12
    strategy._comfyui_config.qwen_image_max_draft_edge = 768
    strategy._config.draft_resolution = (1024, 1024)
    monkeypatch.setattr(
        strategy,
        "_validate_qwen_image_models",
        lambda: ("diffusion.safetensors", "encoder.safetensors", "vae.safetensors"),
    )
    request = GenerationRequest(
        prompt="画一只老虎",
        source_prompt="画一只老虎",
        preset_id="qwen-image",
    )

    workflow = strategy._build_qwen_image_workflow(
        request,
        strategy._config,
        seed=7,
    )

    latent = workflow["7"]["inputs"]
    assert (latent["width"], latent["height"]) == (768, 768)
    assert workflow["8"]["inputs"]["steps"] == 12


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
async def test_reference_image_draft_submits_img2img_workflow(
    strategy, monkeypatch
):
    async def run_inline(func, *args):
        return func(*args)

    monkeypatch.setattr(module.asyncio, "to_thread", run_inline)
    monkeypatch.setattr(strategy, "_check_comfyui", AsyncMock(return_value=None))
    monkeypatch.setattr(
        strategy,
        "_ensure_storage_capacity",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        strategy,
        "_upload_image",
        AsyncMock(return_value="reference.png"),
    )
    submit = AsyncMock(return_value="prompt-reference")
    monkeypatch.setattr(strategy, "_submit_workflow", submit)
    monkeypatch.setattr(strategy, "_record_comfy_job", AsyncMock(return_value=None))
    monkeypatch.setattr(
        strategy,
        "_poll_result",
        AsyncMock(return_value=b"generated-reference"),
    )
    checkpoint = (
        Path(strategy._comfyui_config.models_path)
        / "checkpoints"
        / "approved.safetensors"
    )
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(b"model")
    request = GenerationRequest(
        prompt="a golden retriever running on grass",
        request_id="req-reference",
        reference_images=[
            MediaContent(
                media_type=MediaType.IMAGE,
                raw_data=reference_png(),
                mime_type="image/png",
            )
        ],
    )

    result = await strategy._generate_image_preview_with_comfyui(
        request,
        strategy._config,
        seed=42,
        draft_id="draft-reference",
    )

    assert result == b"generated-reference"
    workflow = submit.await_args.args[0]
    assert workflow["1"]["class_type"] == "LoadImage"
    assert workflow["1"]["inputs"]["image"] == "reference.png"
    assert workflow["7"]["inputs"]["steps"] == 12
    assert workflow["7"]["inputs"]["denoise"] == 0.35
    assert workflow["9"]["inputs"]["filename_prefix"] == (
        "draft_reference_req-reference"
    )


@pytest.mark.asyncio
async def test_reference_image_is_video_keyframe_without_sdxl_regeneration(
    strategy, monkeypatch
):
    async def run_inline(func, *args):
        return func(*args)

    monkeypatch.setattr(module.asyncio, "to_thread", run_inline)
    generate_image = AsyncMock()
    monkeypatch.setattr(
        strategy,
        "_generate_image_preview_with_comfyui",
        generate_image,
    )
    request = GenerationRequest(
        prompt="make the dog run",
        media_type="video",
        reference_images=[
            MediaContent(
                media_type=MediaType.IMAGE,
                raw_data=reference_png(),
                mime_type="image/png",
            )
        ],
    )

    previews = await strategy._generate_video_previews_with_comfyui(
        request,
        strategy._config,
        seed=17,
        draft_id="draft-img2video",
    )

    assert len(previews) == 1
    with Image.open(BytesIO(previews[0])) as preview:
        assert preview.size == (24, 16)
    generate_image.assert_not_awaited()


@pytest.mark.asyncio
async def test_qwen_reference_image_fails_with_actionable_error(
    strategy, monkeypatch
):
    monkeypatch.setattr(strategy, "_check_comfyui", AsyncMock(return_value=None))
    request = GenerationRequest(
        prompt="编辑这张图",
        preset_id="qwen-image",
        reference_images=[
            MediaContent(
                media_type=MediaType.IMAGE,
                raw_data=reference_png(),
            )
        ],
    )

    with pytest.raises(
        DraftWorkflowError,
        match="comfyui_qwen_image_reference_unsupported",
    ):
        await strategy.check_local_dependencies(request)


def test_reference_data_url_rejects_oversized_payload_before_decode(monkeypatch):
    monkeypatch.setattr(impl, "_MAX_REFERENCE_IMAGE_BYTES", 3)

    with pytest.raises(
        DraftWorkflowError,
        match="comfyui_reference_image_too_large",
    ):
        module.DraftGeneratorStrategy._decode_reference_data_url(
            "data:image/png;base64,AAAAAAAA"
        )


@pytest.mark.asyncio
async def test_img2video_preflight_does_not_require_unused_sdxl_checkpoint(
    strategy, monkeypatch
):
    async def run_inline(func, *args):
        return func(*args)

    monkeypatch.setattr(module.asyncio, "to_thread", run_inline)
    monkeypatch.setattr(strategy, "_check_comfyui", AsyncMock(return_value=None))
    models = Path(strategy._comfyui_config.models_path)
    for folder, filename in (
        ("diffusion_models", strategy._comfyui_config.video_diffusion_model),
        ("text_encoders", strategy._comfyui_config.video_text_encoder),
        ("vae", strategy._comfyui_config.video_vae),
    ):
        path = models / folder / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"model")
    request = GenerationRequest(
        prompt="make this dog run",
        media_type="video",
        reference_images=[
            MediaContent(
                media_type=MediaType.IMAGE,
                raw_data=reference_png(),
            )
        ],
    )

    await strategy.check_local_dependencies(request)

    assert not (models / "checkpoints" / "approved.safetensors").exists()


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


@pytest.mark.asyncio
async def test_storage_capacity_caches_models_directory_size(strategy, monkeypatch):
    calls: list[str] = []

    async def run_inline(func, *args):
        return func(*args)

    def directory_size(path: str) -> int:
        calls.append(path)
        return 0

    monkeypatch.setattr(module.asyncio, "to_thread", run_inline)
    monkeypatch.setattr(strategy, "_directory_size", directory_size)
    monkeypatch.setattr(strategy, "_cleanup_expired_outputs", lambda: 0)

    await strategy._ensure_storage_capacity()
    await strategy._ensure_storage_capacity()

    assert calls.count(strategy._comfyui_config.models_path) == 1
    assert calls.count(strategy._comfyui_config.output_path) == 2


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
async def test_poll_results_appends_comfyui_trace_events(strategy, monkeypatch):
    redis = FakeRedis()
    strategy._redis_client = redis

    class Response:
        def __init__(self, payload=None, content=b""):
            self.status_code = 200
            self._payload = payload
            self.content = content

        def json(self):
            return self._payload

    history = {
        "prompt-image": {
            "status": {"messages": []},
            "outputs": {
                "9": {
                    "images": [
                        {
                            "filename": "draft.png",
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
            return Response(content=b"image-bytes")

    monkeypatch.setattr("httpx.AsyncClient", lambda **_kwargs: Client())

    result = await strategy._poll_result(
        "prompt-image",
        timeout=1,
        trace_id="trace-draft",
        draft_id="draft-1",
    )

    assert result == b"image-bytes"
    data = json.loads(redis.hashes["aigateway:trace:trace-draft"]["data"])
    names = [event["name"] for event in data["events"]]
    assert "comfyui.media_downloaded" in names
    assert "comfyui.workflow_completed" in names


@pytest.mark.asyncio
async def test_apply_comfyui_progress_persists_real_step_ratio(strategy):
    redis = FakeRedis()
    strategy._redis_client = redis
    draft = DraftResult(
        draft_id="draft-progress",
        previews=[],
        generation_params={"trace_id": "trace-progress"},
        created_at=1.0,
        expires_at=9999999999.0,
        attempt_number=1,
        max_attempts=5,
        status=DRAFT_STATUS_RUNNING,
        media_type="image",
        session_id="sess-progress",
        progress=0.15,
        stage="running",
        comfy_prompt_id="prompt-progress",
    )
    await strategy._store_draft(draft, ttl_seconds=60)

    await strategy._apply_comfyui_progress(
        "draft-progress",
        "prompt-progress",
        0.25,
        value=3,
        max_value=12,
        stage="running",
        trace_id="trace-progress",
    )

    reloaded = await strategy.get_draft("draft-progress")
    assert reloaded is not None
    assert reloaded.stage == "sampling 3/12"
    assert reloaded.progress == pytest.approx(0.25)
    assert reloaded.generation_params["progress_source"] == "comfyui"
    data = json.loads(redis.hashes["aigateway:trace:trace-progress"]["data"])
    assert "comfyui.progress" in [event["name"] for event in data["events"]]


@pytest.mark.asyncio
async def test_record_comfy_job_resets_previous_workflow_percentage(strategy):
    redis = FakeRedis()
    strategy._redis_client = redis
    draft = DraftResult(
        draft_id="draft-submit",
        previews=[],
        generation_params={"progress_source": "comfyui"},
        created_at=1.0,
        expires_at=9999999999.0,
        status=DRAFT_STATUS_RUNNING,
        media_type="image",
        progress=0.89,
        stage="sampling 12/12",
        comfy_prompt_id="old-prompt",
    )
    await strategy._store_draft(draft, ttl_seconds=60)

    await strategy._record_comfy_job("draft-submit", "new-prompt", "refining")

    reloaded = await strategy.get_draft("draft-submit")
    assert reloaded is not None
    assert reloaded.comfy_prompt_id == "new-prompt"
    assert reloaded.progress == 0.0
    assert reloaded.stage == "waiting_for_comfyui"
    assert reloaded.generation_params["progress_source"] == "comfyui"


@pytest.mark.asyncio
async def test_refine_submission_starts_at_exact_comfyui_zero(strategy, monkeypatch):
    draft = DraftResult(
        draft_id="draft-refine-submit",
        previews=[reference_png()],
        generation_params={
            "prompt": "a dog",
            "seed": 17,
            "quality": "standard",
            "progress_source": "comfyui",
        },
        created_at=1.0,
        expires_at=9999999999.0,
        status=DRAFT_STATUS_RUNNING,
        media_type="image",
        progress=0.89,
        stage="sampling 12/12",
    )
    monkeypatch.setattr(
        strategy,
        "_ensure_storage_capacity",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        strategy,
        "_upload_image",
        AsyncMock(return_value="draft.png"),
    )
    monkeypatch.setattr(strategy, "_build_refine_workflow", lambda **_kwargs: {})
    monkeypatch.setattr(
        strategy,
        "_submit_workflow",
        AsyncMock(return_value="prompt-refine"),
    )
    monkeypatch.setattr(
        strategy,
        "_poll_result",
        AsyncMock(return_value=b"refined"),
    )
    monkeypatch.setattr(
        strategy,
        "_emit_draft_trace",
        AsyncMock(return_value=None),
    )

    result = await strategy._upscale_with_comfyui(draft, (1024, 1024))

    assert result == b"refined"
    assert draft.comfy_prompt_id == "prompt-refine"
    assert draft.stage == "waiting_for_comfyui"
    assert draft.progress == 0.0
    assert draft.generation_params["progress_source"] == "comfyui"


@pytest.mark.asyncio
async def test_comfyui_executing_resets_previous_node_progress(strategy):
    redis = FakeRedis()
    strategy._redis_client = redis
    draft = DraftResult(
        draft_id="draft-node",
        previews=[],
        generation_params={"progress_source": "comfyui"},
        created_at=1.0,
        expires_at=9999999999.0,
        status=DRAFT_STATUS_RUNNING,
        media_type="image",
        progress=1.0,
        stage="sampling 12/12",
        comfy_prompt_id="prompt-node",
    )
    await strategy._store_draft(draft, ttl_seconds=60)

    await strategy._apply_comfyui_executing(
        "draft-node", "prompt-node", node="vae-decode"
    )

    reloaded = await strategy.get_draft("draft-node")
    assert reloaded is not None
    assert reloaded.progress == 0.0
    assert reloaded.stage == "executing vae-decode"
    assert reloaded.generation_params["progress_source"] == "comfyui"


@pytest.mark.asyncio
async def test_postprocess_stage_is_indeterminate_not_invented_percentage(strategy):
    redis = FakeRedis()
    strategy._redis_client = redis
    draft = DraftResult(
        draft_id="draft-finalize",
        previews=[],
        generation_params={"progress_source": "comfyui"},
        created_at=1.0,
        expires_at=9999999999.0,
        status=DRAFT_STATUS_RUNNING,
        media_type="image",
        progress=1.0,
        stage="sampling 12/12",
        comfy_prompt_id="prompt-finalize",
    )
    await strategy._store_draft(draft, ttl_seconds=60)

    await strategy._set_comfyui_postprocess_stage(
        "draft-finalize", "prompt-finalize", stage="finalizing"
    )

    reloaded = await strategy.get_draft("draft-finalize")
    assert reloaded is not None
    assert reloaded.stage == "finalizing"
    assert reloaded.generation_params["progress_source"] == "stage"


@pytest.mark.asyncio
async def test_poll_results_cancels_workflow_when_progress_stalls(
    strategy, monkeypatch
):
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, _url):
            return Response()

    strategy._comfyui_config.progress_stall_timeout = 0
    cancel = AsyncMock(return_value=True)
    monkeypatch.setattr("httpx.AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(strategy, "_cancel_comfyui_workflow", cancel)

    with pytest.raises(DraftWorkflowError, match="comfyui_progress_stalled"):
        await strategy._poll_results("prompt-stalled", timeout=60)

    cancel.assert_awaited_once_with("prompt-stalled", server_url=None)


@pytest.mark.asyncio
async def test_poll_results_retries_transient_history_timeout(strategy, monkeypatch):
    class Response:
        def __init__(self, payload=None, content=b""):
            self.status_code = 200
            self._payload = payload
            self.content = content

        def json(self):
            return self._payload

    history = {
        "prompt-image": {
            "status": {"messages": []},
            "outputs": {
                "9": {
                    "images": [
                        {
                            "filename": "draft.png",
                            "subfolder": "",
                            "type": "output",
                        }
                    ]
                }
            },
        }
    }
    calls = 0

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url):
            nonlocal calls
            if "/history/" in url:
                calls += 1
                if calls == 1:
                    raise httpx.ReadTimeout("busy")
                return Response(history)
            return Response(content=b"image-bytes")

    monkeypatch.setattr("httpx.AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())

    result = await strategy._poll_result("prompt-image", timeout=5)

    assert result == b"image-bytes"
    assert calls == 2


@pytest.mark.asyncio
async def test_poll_results_waits_for_partial_history_outputs(strategy, monkeypatch):
    class Response:
        def __init__(self, payload=None, content=b""):
            self.status_code = 200
            self._payload = payload
            self.content = content

        def json(self):
            return self._payload

    partial = {
        "prompt-image": {
            "status": {"status_str": "success", "completed": False, "messages": []},
            "outputs": {},
        }
    }
    completed = {
        "prompt-image": {
            "status": {"status_str": "success", "completed": True, "messages": []},
            "outputs": {
                "9": {
                    "images": [
                        {
                            "filename": "draft.png",
                            "subfolder": "",
                            "type": "output",
                        }
                    ]
                }
            },
        }
    }
    histories = iter((partial, completed))

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url):
            if "/history/" in url:
                return Response(next(histories))
            return Response(content=b"image-bytes")

    monkeypatch.setattr("httpx.AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())

    result = await strategy._poll_result("prompt-image", timeout=5)

    assert result == b"image-bytes"


@pytest.mark.asyncio
async def test_poll_results_reports_interrupted_workflow(strategy, monkeypatch):
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "prompt-image": {
                    "status": {
                        "status_str": "error",
                        "completed": False,
                        "messages": [["execution_interrupted", {"node_id": "9"}]],
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

    with pytest.raises(
        DraftWorkflowError, match="comfyui_workflow_execution_failed"
    ):
        await strategy._poll_results("prompt-image", timeout=1)


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


@pytest.mark.asyncio
async def test_poll_timeout_interrupts_only_matching_running_workflow(
    strategy, monkeypatch
):
    posts: list[tuple[str, dict | None]] = []

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "queue_running": [[0, "prompt-timeout", {}, {}, []]],
                "queue_pending": [[1, "other-prompt", {}, {}, []]],
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, _url):
            return Response()

        async def post(self, url, json=None):
            posts.append((url, json))
            return Response()

    monkeypatch.setattr("httpx.AsyncClient", lambda **_kwargs: Client())

    with pytest.raises(DraftWorkflowError, match="执行超时"):
        await strategy._poll_results("prompt-timeout", timeout=0)

    assert posts == [
        (f"{strategy._comfyui_config.server_url}/interrupt", None)
    ]
