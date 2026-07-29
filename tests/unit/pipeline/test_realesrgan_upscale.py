from __future__ import annotations

from io import BytesIO
from unittest.mock import AsyncMock, MagicMock

import pytest
from aigateway_core.pipelines.generation._common.config import DraftWorkflowConfig
from aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError
from aigateway_core.pipelines.generation._common.models import GenerationRequest
from aigateway_core.pipelines.generation.draft.draft_generator import (
    DraftGeneratorStrategy,
)
from aigateway_core.shared.integration_configs import ComfyUIConfig
from PIL import Image


def _png(width: int, height: int) -> bytes:
    stream = BytesIO()
    Image.new("RGB", (width, height)).save(stream, format="PNG")
    return stream.getvalue()


def _comfy_config(**overrides) -> ComfyUIConfig:
    values = {
        "checkpoint_name": "sdxl.safetensors",
        "allowed_checkpoints": ["sdxl.safetensors"],
        "upscale_enabled": True,
        "upscale_model": "RealESRGAN_x4plus.pth",
        "allowed_upscale_models": ["RealESRGAN_x4plus.pth"],
    }
    values.update(overrides)
    return ComfyUIConfig(**values)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ((1024, 512), (4096, 2048)),
        ((512, 1024), (2048, 4096)),
        ((800, 800), (4096, 4096)),
    ],
)
def test_faithful_4k_preserves_aspect_ratio(tmp_path, source, expected):
    strategy = DraftGeneratorStrategy(
        DraftWorkflowConfig(store_dir=str(tmp_path)),
        comfyui_config=_comfy_config(max_upscale_long_edge=4096),
    )
    assert strategy._faithful_upscale_resolution(_png(*source)) == expected


def test_faithful_4k_never_downscales_an_oversized_source(tmp_path):
    strategy = DraftGeneratorStrategy(
        DraftWorkflowConfig(store_dir=str(tmp_path)),
        comfyui_config=_comfy_config(max_upscale_long_edge=4096),
    )
    assert strategy._faithful_upscale_resolution(_png(5000, 2500)) == (5000, 2500)


@pytest.mark.asyncio
async def test_video_faithful_4k_does_not_require_image_upscale_model(
    tmp_path,
    monkeypatch,
):
    strategy = DraftGeneratorStrategy(
        DraftWorkflowConfig(store_dir=str(tmp_path / "drafts")),
        comfyui_config=_comfy_config(models_path=str(tmp_path / "models")),
    )
    strategy._check_comfyui = AsyncMock()
    strategy._validate_checkpoint = MagicMock(return_value="sdxl.safetensors")
    strategy._validate_video_models = MagicMock(
        return_value=("wan.safetensors", "umt5.safetensors", "wan_vae.safetensors")
    )
    strategy._validate_upscale_model = MagicMock(
        side_effect=AssertionError("video must not validate RealESRGAN")
    )
    monkeypatch.setattr("os.path.isfile", lambda _path: True)

    await strategy.check_local_dependencies(
        GenerationRequest(
            prompt="animate this image",
            media_type="video",
            quality="faithful_4k",
        )
    )

    strategy._validate_upscale_model.assert_not_called()


def test_faithful_workflow_uses_only_upscale_core_nodes(tmp_path):
    strategy = DraftGeneratorStrategy(
        DraftWorkflowConfig(store_dir=str(tmp_path)),
        comfyui_config=_comfy_config(),
    )
    workflow = strategy._build_faithful_upscale_workflow("input.png", (4096, 2048))
    class_types = {node["class_type"] for node in workflow.values()}
    assert class_types == {
        "LoadImage",
        "UpscaleModelLoader",
        "ImageUpscaleWithModel",
        "ImageScale",
        "SaveImage",
    }
    assert "KSampler" not in class_types
    assert workflow["2"]["inputs"]["model_name"] == "RealESRGAN_x4plus.pth"
    assert workflow["4"]["inputs"]["crop"] == "disabled"


@pytest.mark.parametrize("model", ["../evil.pth", "folder/evil.pth", "unknown.pth"])
def test_upscale_model_must_be_allowlisted_basename(tmp_path, model):
    strategy = DraftGeneratorStrategy(
        DraftWorkflowConfig(store_dir=str(tmp_path)),
        comfyui_config=_comfy_config(upscale_model=model),
    )
    with pytest.raises(DraftWorkflowError):
        strategy._build_faithful_upscale_workflow("input.png", (4096, 4096))


def test_chinese_prompt_prefers_installed_qwen_image(tmp_path):
    models = tmp_path / "models"
    for folder, name in (
        ("diffusion_models", "qwen_image_fp8_e4m3fn.safetensors"),
        ("text_encoders", "qwen_2.5_vl_7b_fp8_scaled.safetensors"),
        ("vae", "qwen_image_vae.safetensors"),
    ):
        (models / folder).mkdir(parents=True, exist_ok=True)
        (models / folder / name).write_bytes(b"model")
    strategy = DraftGeneratorStrategy(
        DraftWorkflowConfig(store_dir=str(tmp_path / "drafts")),
        comfyui_config=_comfy_config(models_path=str(models)),
    )
    request = GenerationRequest(
        prompt="a red sign",
        source_prompt="一块写着中文的红色招牌",
    )

    workflow = strategy._build_image_draft_workflow(request, seed=42)

    assert workflow["2"]["class_type"] == "CLIPLoader"
    assert workflow["2"]["inputs"]["type"] == "qwen_image"
    assert workflow["6"]["inputs"]["text"] == "一块写着中文的红色招牌"
    assert workflow["7"]["class_type"] == "EmptySD3LatentImage"


def test_chinese_prompt_uses_sdxl_when_qwen_is_missing(tmp_path):
    strategy = DraftGeneratorStrategy(
        DraftWorkflowConfig(store_dir=str(tmp_path / "drafts")),
        comfyui_config=_comfy_config(models_path=str(tmp_path / "models")),
    )
    request = GenerationRequest(
        prompt="faithful English translation",
        source_prompt="中文提示词",
    )

    workflow = strategy._build_image_draft_workflow(request, seed=42)

    assert workflow["4"]["class_type"] == "CheckpointLoaderSimple"
    assert workflow["6"]["inputs"]["text"] == "faithful English translation"
