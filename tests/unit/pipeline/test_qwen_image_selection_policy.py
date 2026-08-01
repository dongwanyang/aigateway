from aigateway_core.pipelines.generation._common.config import DraftWorkflowConfig
from aigateway_core.pipelines.generation._common.models import GenerationRequest
from aigateway_core.pipelines.generation.draft.draft_generator import DraftGeneratorStrategy
from aigateway_core.shared.integration_configs import ComfyUIConfig


def make_strategy(*, auto_select: bool = False) -> DraftGeneratorStrategy:
    return DraftGeneratorStrategy(
        config=DraftWorkflowConfig(),
        comfyui_config=ComfyUIConfig(qwen_image_auto_select=auto_select),
    )


def test_chinese_prompt_does_not_implicitly_select_heavy_qwen_workflow():
    strategy = make_strategy()
    request = GenerationRequest(
        prompt="a golden retriever in a park",
        source_prompt="生成一只在公园里的金毛犬",
    )

    assert strategy._should_use_qwen_image(request) is False


def test_explicit_qwen_preset_still_selects_qwen_workflow():
    strategy = make_strategy()
    request = GenerationRequest(
        prompt="一只在公园里的金毛犬",
        preset_id="qwen-image",
    )

    assert strategy._should_use_qwen_image(request) is True


def test_explicit_sdxl_preset_overrides_auto_selection():
    strategy = make_strategy(auto_select=True)
    request = GenerationRequest(
        prompt="一只在公园里的金毛犬",
        preset_id="sdxl-draft",
    )

    assert strategy._should_use_qwen_image(request) is False
