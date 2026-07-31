from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from aigateway_api import gpu_routes
from aigateway_core.prefix.cache import l3_semantic


def load_renderer():
    path = Path(__file__).resolve().parents[2] / "scripts" / "render-deployment-config.py"
    spec = importlib.util.spec_from_file_location("render_deployment_config", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_shared_gpu_renderer_moves_gateway_models_to_cpu(tmp_path: Path) -> None:
    renderer = load_renderer()
    source = Path(__file__).resolve().parents[2] / "config.yaml.template"
    config = renderer.render(
        source,
        edition="full",
        accelerator="cuda",
        embedding_mode="container",
        comfyui_url="http://comfyui:8188",
        embedding_url="",
        monitoring=False,
        shared_gpu=True,
    )
    assert config["embedding"]["device"] == "cpu"
    assert config["deployment"]["shared_gpu"] is True
    assert config["generation_optimization"]["token_compressor"]["clip"]["device"] == "cpu"


def test_gpu_status_does_not_initialize_torch_cuda(monkeypatch: pytest.MonkeyPatch) -> None:
    class Cuda:
        @staticmethod
        def is_initialized() -> bool:
            return False

        @staticmethod
        def current_device() -> int:
            raise AssertionError("status polling initialized CUDA")

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=Cuda()))
    monkeypatch.setattr(
        gpu_routes,
        "_nvidia_smi_status",
        lambda: {
            "available": True,
            "device": "0",
            "name": "Test GPU",
            "device_used_bytes": 2,
            "device_free_bytes": 3,
            "device_total_bytes": 5,
            "device_memory_source": "nvidia-smi",
        },
    )
    status = gpu_routes._gateway_cuda_status()
    assert status["available"] is True
    assert status["torch_initialized"] is False
    assert status["allocated_bytes"] == 0
    assert status["reserved_bytes"] == 0
    assert status["device_used_bytes"] == 2


def test_release_l3_model_clears_cached_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    class Model:
        def __init__(self) -> None:
            self.devices: list[str] = []

        def to(self, device: str) -> "Model":
            self.devices.append(device)
            return self

    model = Model()
    l3_semantic._l3_model_cache.update({"model": model, "tokenizer": object(), "device": "cuda"})
    monkeypatch.setattr(l3_semantic.gc, "collect", lambda: 0)
    assert l3_semantic.release_l3_model() is True
    assert l3_semantic._l3_model_cache == {}
    assert model.devices == ["cpu"]


@pytest.mark.asyncio
async def test_gpu_release_rejects_active_comfyui_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gpu_routes,
        "_probe",
        lambda _request: asyncio.sleep(0, result={"available": True, "queue": {"running": 1, "pending": 0}}),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(config_manager=None)))
    with pytest.raises(Exception) as exc_info:
        await gpu_routes.release_gpu_memory(request, {})
    assert getattr(exc_info.value, "status_code", None) == 409
