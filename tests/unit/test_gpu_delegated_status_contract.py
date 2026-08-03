"""Regression coverage for shared-pool GPU status normalization."""
from __future__ import annotations

from aigateway_api.gpu_routes import (
    _execution_gpu_status,
    _normalize_gateway_topology,
)


def test_valid_shared_pool_is_not_reported_as_comfyui_delegation() -> None:
    scheduler = {
        "enabled": True,
        "devices": [{"uuid": "GPU-a"}],
        "workers": [
            {
                "worker_id": "comfyui-gpu-0",
                "device_uuid": "GPU-a",
                "capabilities": ["image", "video", "upscale"],
                "healthy": True,
            }
        ],
    }
    status = _normalize_gateway_topology(
        {
            "available": False,
            "torch_initialized": False,
            "cuda_disabled": False,
            "error": "gpu_status_unavailable",
        },
        comfy_available=True,
        scheduler=scheduler,
        pool_expected=True,
    )
    execution = _execution_gpu_status(
        status,
        comfy_available=True,
        normalized_comfy_memory=None,
        scheduler=scheduler,
        pool_expected=True,
    )

    assert status["available"] is True
    assert status["local_cuda_available"] is False
    assert status["status"] == "scheduler_pool"
    assert status["delegated_to"] is None
    assert status["cuda_disabled"] is False
    assert status["error"] is None
    assert execution["available"] is True
    assert execution["mode"] == "scheduler_pool"
    assert execution["owner"] == "scheduler"


def test_pool_mode_precedes_raw_gateway_allocator_status() -> None:
    scheduler = {
        "enabled": True,
        "devices": [{"uuid": "GPU-a"}],
        "workers": [
            {
                "worker_id": "comfyui-gpu-0",
                "device_uuid": "GPU-a",
                "capabilities": ["image"],
                "healthy": True,
            }
        ],
    }
    status = _normalize_gateway_topology(
        {
            "available": True,
            "torch_initialized": False,
            "cuda_disabled": False,
            "device_total_bytes": 16_000,
            "device_free_bytes": 15_000,
            "device_used_bytes": 1_000,
        },
        comfy_available=True,
        scheduler=scheduler,
        pool_expected=True,
    )
    execution = _execution_gpu_status(
        status,
        comfy_available=True,
        normalized_comfy_memory=None,
        scheduler=scheduler,
        pool_expected=True,
    )

    assert status["status"] == "scheduler_pool"
    assert status["local_cuda_available"] is True
    assert execution["mode"] == "scheduler_pool"
    assert execution["owner"] == "scheduler"


def test_enabled_pool_with_missing_inventory_is_an_error() -> None:
    scheduler = {
        "enabled": True,
        "devices": [],
        "workers": [
            {
                "worker_id": "comfyui-gpu-0",
                "device_uuid": "GPU-a",
                "capabilities": ["image"],
                "healthy": True,
            }
        ],
    }
    status = _normalize_gateway_topology(
        {
            "available": False,
            "torch_initialized": False,
            "cuda_disabled": False,
            "error": "gpu_status_unavailable",
        },
        comfy_available=True,
        scheduler=scheduler,
        pool_expected=True,
    )
    execution = _execution_gpu_status(
        status,
        comfy_available=True,
        normalized_comfy_memory=None,
        scheduler=scheduler,
        pool_expected=True,
    )

    assert status["available"] is False
    assert status["status"] == "scheduler_error"
    assert status["delegated_to"] is None
    assert status["cuda_disabled"] is False
    assert status["error"] == "gpu_scheduler_topology_incomplete"
    assert execution["available"] is False
    assert execution["mode"] == "scheduler_error"
