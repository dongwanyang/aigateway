"""One-shot alignment of legacy tests with PR #26 runtime contracts."""
from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"anchor not found in {path}: {old[:80]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


router_test = "tests/unit/bridge/test_model_router_strategy.py"
for old, new in (
    (
        '"model-a": {"prompt": 0.01, "completion": 0.5}',
        '"model-a": {"prompt": 0.000005, "completion": 0.00001}',
    ),
    (
        '"model-b": {"prompt": 0.05, "completion": 1.0}',
        '"model-b": {"prompt": 0.000025, "completion": 0.00005}',
    ),
    (
        '"model-c": {"prompt": 0.10, "completion": 2.0}',
        '"model-c": {"prompt": 0.00005, "completion": 0.0001}',
    ),
    (
        '"model-d": {"prompt": 0.03, "completion": 0.8}',
        '"model-d": {"prompt": 0.000015, "completion": 0.00003}',
    ),
    (
        '"model-e": {"prompt": 0.08, "completion": 1.5}',
        '"model-e": {"prompt": 0.00004, "completion": 0.00008}',
    ),
    (
        "assert model_a.price_per_request == 0.01",
        "assert model_a.price_per_request == pytest.approx(0.01)",
    ),
    (
        "assert decision.estimated_cost == 0.03",
        "assert decision.estimated_cost == pytest.approx(0.03)",
    ),
    (
        "assert decision.estimated_cost == 0.10",
        "assert decision.estimated_cost == pytest.approx(0.10)",
    ),
):
    replace_once(router_test, old, new)

replace_once(
    "tests/unit/streaming/test_streaming.py",
    'assert "Something went wrong" in err_data["error"]["message"]',
    'assert err_data["error"]["message"] == "The response stream terminated unexpectedly."\n'
    '        assert "Something went wrong" not in err_data["error"]["message"]',
)

replace_once(
    "tests/unit/test_qa_report_2026_08_02_regressions.py",
    'comfyui_config=ComfyUIConfig(workflow_version="test"),',
    'comfyui_config=ComfyUIConfig(\n'
    '            workflow_version="test",\n'
    '            scheduler_managed=True,\n'
    '        ),',
)

gpu_routes = "aigateway-api/src/aigateway_api/gpu_routes.py"
replace_once(
    gpu_routes,
    '        capabilities = override.get("capabilities")\n'
    '        if not isinstance(capabilities, list):\n'
    '            capabilities = worker.get("capabilities") or []\n'
    '        if not isinstance(capabilities, list) or not capabilities:\n'
    '            continue\n',
    '        capabilities = override.get("capabilities")\n'
    '        if not isinstance(capabilities, list):\n'
    '            worker_capabilities = worker.get("capabilities")\n'
    '            capabilities = (\n'
    '                worker_capabilities\n'
    '                if isinstance(worker_capabilities, list)\n'
    '                else ["image", "video", "upscale"]\n'
    '            )\n'
    '        if not capabilities:\n'
    '            continue\n',
)
replace_once(
    gpu_routes,
    '        capabilities = override.get("capabilities")\n'
    '        if not isinstance(capabilities, list):\n'
    '            capabilities = worker.get("capabilities") or []\n'
    '        if isinstance(capabilities, list):\n'
    '            result.update(str(item) for item in capabilities if item)\n',
    '        capabilities = override.get("capabilities")\n'
    '        if not isinstance(capabilities, list):\n'
    '            worker_capabilities = worker.get("capabilities")\n'
    '            capabilities = (\n'
    '                worker_capabilities\n'
    '                if isinstance(worker_capabilities, list)\n'
    '                else ["image", "video", "upscale"]\n'
    '            )\n'
    '        result.update(str(item) for item in capabilities if item)\n',
)
