from pathlib import Path

import pytest
from aigateway_core.pipelines.generation._common.config import DraftWorkflowConfig
from aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError
from aigateway_core.pipelines.generation._common.models import GenerationRequest
from aigateway_core.pipelines.generation.draft.draft_generator import (
    DraftGeneratorStrategy,
)
from aigateway_core.shared.comfyui_model_discovery import checkpoint_preset_id
from aigateway_core.shared.integration_configs import ComfyUIConfig


def make_strategy(
    tmp_path: Path,
    *,
    auto_select: bool = False,
    enabled: bool = True,
) -> DraftGeneratorStrategy:
    return DraftGeneratorStrategy(
        config=DraftWorkflowConfig(store_dir=str(tmp_path / "drafts")),
        comfyui_config=ComfyUIConfig(
            qwen_image_auto_select=auto_select,
            qwen_image_enabled=enabled,
        ),
    )


def test_chinese_prompt_does_not_implicitly_select_heavy_qwen_workflow(tmp_path):
    strategy = make_strategy(tmp_path)
    request = GenerationRequest(
        prompt="a golden retriever in a park",
        source_prompt="生成一只在公园里的金毛犬",
    )

    assert strategy._should_use_qwen_image(request) is False


def test_explicit_qwen_preset_still_selects_qwen_workflow(tmp_path):
    strategy = make_strategy(tmp_path)
    request = GenerationRequest(
        prompt="一只在公园里的金毛犬",
        preset_id="qwen-image",
    )

    assert strategy._should_use_qwen_image(request) is True


def test_explicit_sdxl_preset_overrides_auto_selection(tmp_path):
    strategy = make_strategy(tmp_path, auto_select=True)
    request = GenerationRequest(
        prompt="一只在公园里的金毛犬",
        preset_id="sdxl-draft",
    )

    assert strategy._should_use_qwen_image(request) is False


def test_explicit_qwen_preset_respects_disabled_policy(tmp_path):
    strategy = make_strategy(tmp_path, enabled=False)
    request = GenerationRequest(prompt="一只金毛犬", preset_id="qwen-image")

    with pytest.raises(DraftWorkflowError, match="comfyui_qwen_image_disabled"):
        strategy._should_use_qwen_image(request)


def test_qwen_model_validation_respects_disabled_policy(tmp_path):
    strategy = make_strategy(tmp_path, enabled=False)

    with pytest.raises(DraftWorkflowError, match="comfyui_qwen_image_disabled"):
        strategy._validate_qwen_image_models()


def test_discovered_checkpoint_preset_selects_installed_model(tmp_path):
    models = tmp_path / "models"
    (models / "checkpoints" / "portraits").mkdir(parents=True)
    selected = "portraits/cinematic.safetensors"
    (models / "checkpoints" / selected).write_bytes(b"model")
    strategy = DraftGeneratorStrategy(
        config=DraftWorkflowConfig(store_dir=str(tmp_path / "drafts")),
        comfyui_config=ComfyUIConfig(
            models_path=str(models),
            checkpoint_name="default.safetensors",
            allowed_checkpoints=["default.safetensors"],
        ),
    )
    request = GenerationRequest(
        prompt="cinematic portrait",
        preset_id=checkpoint_preset_id(selected),
    )

    workflow = strategy._build_image_draft_workflow(request, seed=42)

    assert workflow["4"]["inputs"]["ckpt_name"] == selected


def test_unknown_image_preset_does_not_silently_fall_back(tmp_path):
    strategy = make_strategy(tmp_path)

    with pytest.raises(DraftWorkflowError, match="comfyui_unknown_image_preset"):
        strategy._build_image_draft_workflow(
            GenerationRequest(prompt="test", preset_id="unregistered-model")
        )
