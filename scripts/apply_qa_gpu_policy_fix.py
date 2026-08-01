from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    if old not in text:
        if new in text:
            return
        raise RuntimeError(f"pattern missing: {path}")
    if text.count(old) != 1:
        raise RuntimeError(f"pattern not unique: {path}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


replace_once(
    "aigateway-core/src/aigateway_core/shared/integration_configs.py",
    "    qwen_image_enabled: bool = True\n    qwen_image_diffusion_model: str = \"qwen_image_fp8_e4m3fn.safetensors\"\n",
    "    qwen_image_enabled: bool = True\n    qwen_image_auto_select: bool = False\n    qwen_image_diffusion_model: str = \"qwen_image_fp8_e4m3fn.safetensors\"\n",
)

replace_once(
    "aigateway-core/src/aigateway_core/pipelines/generation/registration.py",
    '''        qwen_image_enabled=comfyui_dict.get(\n            "qwen_image_enabled", ComfyUIConfig.qwen_image_enabled\n        ),\n        qwen_image_diffusion_model=comfyui_dict.get(\n''',
    '''        qwen_image_enabled=comfyui_dict.get(\n            "qwen_image_enabled", ComfyUIConfig.qwen_image_enabled\n        ),\n        qwen_image_auto_select=comfyui_dict.get(\n            "qwen_image_auto_select", ComfyUIConfig.qwen_image_auto_select\n        ),\n        qwen_image_diffusion_model=comfyui_dict.get(\n''',
)

replace_once(
    "aigateway-core/src/aigateway_core/pipelines/generation/draft/_draft_generator_impl.py",
    '''    def _should_use_qwen_image(self, request: GenerationRequest) -> bool:\n        if request.preset_id == "qwen-image":\n            return True\n        source_prompt = request.source_prompt or request.prompt\n        return bool(re.search(r"[\\u3400-\\u9fff]", source_prompt)) and (\n            self._qwen_image_models_installed()\n        )\n''',
    '''    def _should_use_qwen_image(self, request: GenerationRequest) -> bool:\n        # Qwen-Image is substantially heavier than the default SDXL draft path.\n        # Never select it solely from prompt language unless the deployment has\n        # explicitly opted into that policy. An explicit preset always wins.\n        if request.preset_id:\n            return request.preset_id == "qwen-image"\n        if not self._comfyui_config.qwen_image_auto_select:\n            return False\n        source_prompt = request.source_prompt or request.prompt\n        return bool(re.search(r"[\\u3400-\\u9fff]", source_prompt)) and (\n            self._qwen_image_models_installed()\n        )\n''',
)

replace_once(
    "config.yaml.template",
    '''      qwen_image_enabled: true\n      qwen_image_diffusion_model: "qwen_image_fp8_e4m3fn.safetensors"\n''',
    '''      qwen_image_enabled: true\n      qwen_image_auto_select: false  # T4 默认走 SDXL；仅显式选择 Qwen 或主动开启自动选择\n      qwen_image_diffusion_model: "qwen_image_fp8_e4m3fn.safetensors"\n''',
)

write(
    "tests/unit/pipeline/test_qwen_image_selection_policy.py",
    '''from aigateway_core.pipelines.generation._common.config import DraftWorkflowConfig\nfrom aigateway_core.pipelines.generation._common.models import GenerationRequest\nfrom aigateway_core.pipelines.generation.draft.draft_generator import DraftGeneratorStrategy\nfrom aigateway_core.shared.integration_configs import ComfyUIConfig\n\n\ndef make_strategy(*, auto_select: bool = False) -> DraftGeneratorStrategy:\n    return DraftGeneratorStrategy(\n        config=DraftWorkflowConfig(),\n        comfyui_config=ComfyUIConfig(qwen_image_auto_select=auto_select),\n    )\n\n\ndef test_chinese_prompt_does_not_implicitly_select_heavy_qwen_workflow():\n    strategy = make_strategy()\n    request = GenerationRequest(\n        prompt="a golden retriever in a park",\n        source_prompt="生成一只在公园里的金毛犬",\n    )\n\n    assert strategy._should_use_qwen_image(request) is False\n\n\ndef test_explicit_qwen_preset_still_selects_qwen_workflow():\n    strategy = make_strategy()\n    request = GenerationRequest(\n        prompt="一只在公园里的金毛犬",\n        preset_id="qwen-image",\n    )\n\n    assert strategy._should_use_qwen_image(request) is True\n\n\ndef test_explicit_sdxl_preset_overrides_auto_selection():\n    strategy = make_strategy(auto_select=True)\n    request = GenerationRequest(\n        prompt="一只在公园里的金毛犬",\n        preset_id="sdxl-draft",\n    )\n\n    assert strategy._should_use_qwen_image(request) is False\n''',
)

print("GPU-safe Qwen selection policy applied")
