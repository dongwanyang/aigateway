from pathlib import Path
from types import SimpleNamespace

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
            allowed_checkpoints=["default.safetensors", selected],
            checkpoint_vram_gb={selected: 10},
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

def test_dynamic_checkpoint_must_be_allowlisted(tmp_path):
    models = tmp_path / "models"
    (models / "checkpoints").mkdir(parents=True)
    selected = "private.safetensors"
    (models / "checkpoints" / selected).write_bytes(b"model")
    strategy = DraftGeneratorStrategy(
        config=DraftWorkflowConfig(store_dir=str(tmp_path / "drafts")),
        comfyui_config=ComfyUIConfig(
            models_path=str(models),
            checkpoint_name="default.safetensors",
            allowed_checkpoints=["default.safetensors"],
            checkpoint_vram_gb={selected: 10},
        ),
    )

    with pytest.raises(DraftWorkflowError, match="checkpoint_not_allowed"):
        strategy._checkpoint_for_request(
            GenerationRequest(
                prompt="test",
                preset_id=checkpoint_preset_id(selected),
            )
        )


def test_dynamic_checkpoint_requires_server_vram_budget(tmp_path):
    models = tmp_path / "models"
    (models / "checkpoints").mkdir(parents=True)
    selected = "heavy.safetensors"
    (models / "checkpoints" / selected).write_bytes(b"model")
    strategy = DraftGeneratorStrategy(
        config=DraftWorkflowConfig(store_dir=str(tmp_path / "drafts")),
        comfyui_config=ComfyUIConfig(
            models_path=str(models),
            checkpoint_name="default.safetensors",
            allowed_checkpoints=["default.safetensors", selected],
        ),
    )

    with pytest.raises(DraftWorkflowError, match="vram_unconfigured"):
        strategy._checkpoint_for_request(
            GenerationRequest(
                prompt="test",
                preset_id=checkpoint_preset_id(selected),
            )
        )


def test_client_vram_hint_cannot_lower_server_minimum(tmp_path):
    models = tmp_path / "models"
    (models / "checkpoints").mkdir(parents=True)
    selected = "heavy.safetensors"
    (models / "checkpoints" / selected).write_bytes(b"model")
    strategy = DraftGeneratorStrategy(
        config=DraftWorkflowConfig(store_dir=str(tmp_path / "drafts")),
        comfyui_config=ComfyUIConfig(
            models_path=str(models),
            checkpoint_name="default.safetensors",
            allowed_checkpoints=["default.safetensors", selected],
            checkpoint_vram_gb={selected: 14},
        ),
    )
    request = GenerationRequest(
        prompt="test",
        preset_id=checkpoint_preset_id(selected),
        required_vram_gb=2,
    )

    assert strategy._draft_required_vram_for_request(
        request, uses_qwen_image=False
    ) == 14


def test_refine_pins_persisted_checkpoint_and_fails_if_revoked(tmp_path):
    models = tmp_path / "models"
    (models / "checkpoints").mkdir(parents=True)
    original = "original.safetensors"
    replacement = "replacement.safetensors"
    (models / "checkpoints" / original).write_bytes(b"original")
    (models / "checkpoints" / replacement).write_bytes(b"replacement")
    config = ComfyUIConfig(
        models_path=str(models),
        checkpoint_name=replacement,
        allowed_checkpoints=[original, replacement],
        checkpoint_vram_gb={original: 10},
    )
    strategy = DraftGeneratorStrategy(
        config=DraftWorkflowConfig(store_dir=str(tmp_path / "drafts")),
        comfyui_config=config,
    )
    draft = SimpleNamespace(
        generation_params={
            "preset_id": "sdxl-draft",
            "checkpoint": original,
        }
    )

    assert strategy._checkpoint_for_refine(draft) == original

    config.allowed_checkpoints = [replacement]
    with pytest.raises(DraftWorkflowError, match="checkpoint_not_allowed"):
        strategy._checkpoint_for_refine(draft)


@pytest.mark.asyncio
async def test_video_preflight_validates_keyframe_checkpoint(tmp_path, monkeypatch):
    models = tmp_path / "models"
    for folder in ("checkpoints", "diffusion_models", "text_encoders", "vae"):
        (models / folder).mkdir(parents=True, exist_ok=True)
    config = ComfyUIConfig(
        models_path=str(models),
        checkpoint_name="missing-default.safetensors",
        allowed_checkpoints=["missing-default.safetensors"],
    )
    for folder, name in (
        ("diffusion_models", config.video_diffusion_model),
        ("text_encoders", config.video_text_encoder),
        ("vae", config.video_vae),
    ):
        (models / folder / name).write_bytes(b"model")
    strategy = DraftGeneratorStrategy(
        config=DraftWorkflowConfig(store_dir=str(tmp_path / "drafts")),
        comfyui_config=config,
    )

    async def healthy_comfyui():
        return None

    monkeypatch.setattr(strategy, "_check_comfyui", healthy_comfyui)

    with pytest.raises(DraftWorkflowError, match="checkpoints/missing-default"):
        await strategy.check_local_dependencies(
            GenerationRequest(
                prompt="animate this",
                media_type="video",
                preset_id="wan2.2-ti2v-5b",
            )
        )


def test_video_confirmation_budget_cannot_be_lowered_by_client(tmp_path):
    strategy = make_strategy(tmp_path)
    draft = SimpleNamespace(
        media_type="video",
        generation_params={
            "required_vram_gb": 2,
            "required_vram_explicit": True,
            "quality": "standard",
        },
    )

    assert strategy._confirmation_required_vram_gb(draft) == 12
