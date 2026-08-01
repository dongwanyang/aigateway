from pathlib import Path

from aigateway_core.pipelines.generation._common.config import DraftWorkflowConfig
from aigateway_core.pipelines.generation._common.models import GenerationRequest
from aigateway_core.pipelines.generation.draft.draft_generator import DraftGeneratorStrategy
from aigateway_core.shared.integration_configs import ComfyUIConfig


def make_strategy(
    tmp_path: Path, *, auto_select: bool = False
) -> DraftGeneratorStrategy:
    return DraftGeneratorStrategy(
        config=DraftWorkflowConfig(store_dir=str(tmp_path / "drafts")),
        comfyui_config=ComfyUIConfig(qwen_image_auto_select=auto_select),
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
    import pytest

    from aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError

    strategy = DraftGeneratorStrategy(
        config=DraftWorkflowConfig(store_dir=str(tmp_path / "drafts")),
        comfyui_config=ComfyUIConfig(qwen_image_enabled=False),
    )
    request = GenerationRequest(prompt="一只金毛犬", preset_id="qwen-image")

    with pytest.raises(DraftWorkflowError, match="comfyui_qwen_image_disabled"):
        strategy._should_use_qwen_image(request)
