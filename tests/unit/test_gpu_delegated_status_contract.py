"""Regression coverage for delegated ComfyUI GPU status rendering."""
from __future__ import annotations

from aigateway_api.gpu_routes import _normalize_gateway_topology


def test_delegated_comfyui_topology_sets_control_panel_compatibility_flag() -> None:
    status = _normalize_gateway_topology(
        {
            "available": False,
            "torch_initialized": False,
            "cuda_disabled": False,
            "error": "gpu_status_unavailable",
        },
        comfy_available=True,
        scheduler={"devices": [], "workers": []},
    )

    assert status["available"] is False
    assert status["local_cuda_available"] is False
    assert status["status"] == "delegated"
    assert status["delegated_to"] == "comfyui"
    assert status["cuda_disabled"] is True
    assert status["error"] is None
