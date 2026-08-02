from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self

import httpx
import pytest
from aigateway_api import gpu_routes
from aigateway_core.prefix.cache import l3_semantic
from aigateway_core.shared import gpu_memory
from fastapi import APIRouter


@pytest.fixture(autouse=True)
def _inline_thread_offload(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep these unit tests deterministic without owning executor threads."""

    async def _run_inline(func, *args: object, **kwargs: object) -> object:
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _run_inline)


def load_renderer():
    path = Path(__file__).resolve().parents[2] / "scripts" / "render-deployment-config.py"
    spec = importlib.util.spec_from_file_location("render_deployment_config", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_shared_gpu_renderer_enables_dynamic_pool_without_static_cpu_split() -> None:
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
    assert config["embedding"]["device"] == "auto"
    assert config["deployment"]["shared_gpu"] is True
    assert config["generation_optimization"]["token_compressor"]["clip"]["device"] == "auto"
    assert config["gpu_scheduler"]["enabled"] is True


def test_nvidia_smi_status_selects_visible_device(monkeypatch: pytest.MonkeyPatch) -> None:
    completed = SimpleNamespace(
        returncode=0,
        stdout="0, GPU A, 100, 900, 1000\n1, GPU B, 200, 1800, 2000\n",
    )
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "1")
    monkeypatch.setattr(gpu_memory.subprocess, "run", lambda *args, **kwargs: completed)
    status = gpu_memory.nvidia_smi_status()
    assert status == {
        "available": True,
        "device": "1",
        "name": "GPU B",
        "device_used_bytes": 200 * 1024 * 1024,
        "device_free_bytes": 1800 * 1024 * 1024,
        "device_total_bytes": 2000 * 1024 * 1024,
        "device_memory_source": "nvidia-smi",
    }


def test_nvidia_smi_status_handles_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*args: object, **kwargs: object) -> object:
        raise FileNotFoundError

    monkeypatch.setattr(gpu_memory.subprocess, "run", missing)
    assert gpu_memory.nvidia_smi_status() is None


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
        gpu_memory,
        "nvidia_smi_status",
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
    status = gpu_memory.gateway_cuda_status()
    assert status["available"] is True
    assert status["torch_initialized"] is False
    assert status["allocated_bytes"] == 0
    assert status["reserved_bytes"] == 0
    assert status["device_used_bytes"] == 2


def test_gpu_status_reports_intentionally_disabled_cuda(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Cuda:
        @staticmethod
        def is_initialized() -> bool:
            return False

    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "-1")
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=Cuda()))
    monkeypatch.setattr(gpu_memory, "nvidia_smi_status", lambda: None)

    status = gpu_memory.gateway_cuda_status()

    assert status["available"] is False
    assert status["torch_initialized"] is False
    assert status["cuda_disabled"] is True


def test_gpu_status_reads_allocator_after_cuda_is_initialized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Cuda:
        @staticmethod
        def is_initialized() -> bool:
            return True

        @staticmethod
        def current_device() -> int:
            return 0

        @staticmethod
        def get_device_name(device: int) -> str:
            assert device == 0
            return "Torch GPU"

        @staticmethod
        def memory_allocated(device: int) -> int:
            return 11

        @staticmethod
        def memory_reserved(device: int) -> int:
            return 17

        @staticmethod
        def mem_get_info(device: int) -> tuple[int, int]:
            return 30, 100

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=Cuda()))
    monkeypatch.setattr(gpu_memory, "nvidia_smi_status", lambda: None)
    status = gpu_memory.gateway_cuda_status()
    assert status["torch_initialized"] is True
    assert status["name"] == "Torch GPU"
    assert status["allocated_bytes"] == 11
    assert status["reserved_bytes"] == 17
    assert status["device_used_bytes"] == 70
    assert status["device_memory_source"] == "torch"


def test_comfy_memory_and_diagnosis() -> None:
    comfy = gpu_memory.comfy_memory({"vram_total": 100, "vram_free": 25})
    assert comfy and comfy["used_bytes"] == 75
    assert gpu_memory.comfy_memory(None) is None
    findings = gpu_memory.diagnose_memory(
        {
            "torch_initialized": True,
            "allocated_bytes": 20,
            "reserved_bytes": 40,
        },
        comfy,
        True,
        True,
    )
    assert findings == [
        "gateway_and_comfyui_share_one_gpu",
        "gateway_pytorch_cache_reserved",
        "gateway_model_memory_resident",
        "comfyui_idle_with_resident_models",
    ]


def test_failed_l3_model_load_does_not_poison_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    class Tokenizer:
        @staticmethod
        def from_pretrained(*args: object, **kwargs: object) -> object:
            return object()

    class Model:
        @staticmethod
        def from_pretrained(*args: object, **kwargs: object) -> object:
            raise RuntimeError("model load failed")

    class Cuda:
        @staticmethod
        def is_available() -> bool:
            return False

        @staticmethod
        def is_initialized() -> bool:
            return False

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=Cuda()))
    monkeypatch.setitem(
        sys.modules,
        "transformers",
        SimpleNamespace(AutoModel=Model, AutoTokenizer=Tokenizer),
    )
    monkeypatch.setattr(l3_semantic, "_l3_device", "cpu")
    l3_semantic._l3_model_cache.clear()
    assert l3_semantic._compute_l3_vector_sync("hello") is None
    assert l3_semantic._l3_model_cache == {}


def test_release_l3_model_clears_cached_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    class Model:
        def __init__(self) -> None:
            self.devices: list[str] = []

        def to(self, device: str) -> Model:
            self.devices.append(device)
            return self

    model = Model()
    l3_semantic._l3_model_cache.update({"model": model, "tokenizer": object(), "device": "cuda"})
    monkeypatch.setattr(l3_semantic.gc, "collect", lambda: 0)
    assert l3_semantic.release_l3_model() is True
    assert l3_semantic._l3_model_cache == {}
    assert model.devices == ["cpu"]


@pytest.mark.asyncio
async def test_gpu_status_route_reports_partial_comfyui_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Manager:
        def get(self, path: str, default: object) -> object:
            if path == "deployment":
                return {"shared_gpu": True}
            return default

    monkeypatch.setattr(
        gpu_routes,
        "gateway_cuda_status",
        lambda: {
            "available": True,
            "torch_initialized": True,
            "allocated_bytes": 10,
            "reserved_bytes": 20,
        },
    )
    monkeypatch.setattr(
        gpu_routes,
        "_probe",
        lambda request: asyncio.sleep(
            0,
            result={
                "available": True,
                "gpu": {"vram_total": 100, "vram_free": 40},
                "queue": {"running": 0, "pending": 0},
                "endpoint_errors": {"object_info": "ReadTimeout"},
            },
        ),
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(config_manager=Manager()))
    )
    response = await gpu_routes.get_gpu_status(request, {})
    data = response["data"]
    assert data["queue_idle"] is True
    assert data["shared_gpu"] is True
    assert data["comfyui"]["memory"]["used_bytes"] == 60
    assert data["comfyui"]["endpoint_errors"] == {"object_info": "ReadTimeout"}
    assert "gateway_pytorch_cache_reserved" in data["diagnosis"]


@pytest.mark.asyncio
async def test_gpu_release_rejects_active_comfyui_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        gpu_routes,
        "_probe",
        lambda request: asyncio.sleep(
            0,
            result={"available": True, "queue": {"running": 1, "pending": 0}},
        ),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(config_manager=None)))
    with pytest.raises(Exception) as exc_info:
        await gpu_routes.release_gpu_memory(request, {})
    assert getattr(exc_info.value, "status_code", None) == 409


@pytest.mark.asyncio
async def test_gpu_release_never_frees_comfyui_when_queue_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Manager:
        def get(self, path: str, default: object) -> object:
            if path.endswith("comfyui"):
                return {"server_url": "http://comfyui:8188"}
            return default

    async def release_gateway() -> dict[str, bool]:
        return {"l3_embedding": True, "rag_embedding": False}

    async def forbidden_release(server_url: str) -> dict[str, Any]:
        raise AssertionError(f"ComfyUI /free must not be called: {server_url}")

    monkeypatch.setattr(
        gpu_routes,
        "_probe",
        lambda request: asyncio.sleep(0, result={"available": True, "queue": None}),
    )
    monkeypatch.setattr(gpu_routes, "_release_gateway_models", release_gateway)
    monkeypatch.setattr(gpu_routes, "_release_comfyui", forbidden_release)
    monkeypatch.setattr(
        gpu_routes,
        "gateway_cuda_status",
        lambda: {"available": False, "allocated_bytes": 0, "reserved_bytes": 0},
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(config_manager=Manager()))
    )
    response = await gpu_routes.release_gpu_memory(request, {})
    assert response["data"]["gateway_models"]["l3_embedding"] is True
    assert response["data"]["comfyui"] == {
        "requested": False,
        "released": False,
        "skipped": "queue_status_unknown",
    }


@pytest.mark.asyncio
async def test_gpu_release_frees_gateway_and_idle_comfyui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Manager:
        def get(self, path: str, default: object) -> object:
            if path.endswith("comfyui"):
                return {"server_url": "http://comfyui:8188"}
            return default

    async def release_gateway() -> dict[str, bool]:
        return {"l3_embedding": True, "rag_embedding": True}

    async def release_comfyui(server_url: str) -> dict[str, Any]:
        assert server_url == "http://comfyui:8188"
        return {"requested": True, "released": True}

    monkeypatch.setattr(
        gpu_routes,
        "_probe",
        lambda request: asyncio.sleep(
            0,
            result={"available": True, "queue": {"running": 0, "pending": 0}},
        ),
    )
    monkeypatch.setattr(gpu_routes, "_release_gateway_models", release_gateway)
    monkeypatch.setattr(gpu_routes, "_release_comfyui", release_comfyui)
    monkeypatch.setattr(
        gpu_routes,
        "gateway_cuda_status",
        lambda: {"available": True, "allocated_bytes": 0, "reserved_bytes": 0},
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(config_manager=Manager()))
    )
    response = await gpu_routes.release_gpu_memory(request, {})
    assert response["data"]["gateway_models"] == {
        "l3_embedding": True,
        "rag_embedding": True,
    }
    assert response["data"]["comfyui"]["released"] is True


@pytest.mark.asyncio
async def test_release_comfyui_reports_http_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class Client:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, *args: object, **kwargs: object) -> object:
            raise httpx.ConnectError("unavailable")

    monkeypatch.setattr(gpu_routes.httpx, "AsyncClient", lambda **kwargs: Client())
    result = await gpu_routes._release_comfyui("http://comfyui:8188")
    assert result == {
        "requested": True,
        "released": False,
        "error": "ConnectError",
    }


def test_gpu_routes_install_once() -> None:
    target = APIRouter()
    gpu_routes.install_gpu_routes(target)
    first_count = len(target.routes)
    assert first_count == len(gpu_routes.router.routes)
    gpu_routes.install_gpu_routes(target)
    assert len(target.routes) == first_count

@pytest.mark.asyncio
async def test_failed_l3_inference_rearms_idle_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduled: list[bool] = []
    monkeypatch.setattr(
        l3_semantic,
        "_compute_l3_vector_sync",
        lambda text, load_if_missing=True: None,
    )
    monkeypatch.setattr(
        l3_semantic,
        "_schedule_idle_release",
        lambda: scheduled.append(True),
    )
    monkeypatch.setattr(l3_semantic, "_invalidate_idle_release", lambda: None)

    assert await l3_semantic._compute_l3_vector("broken inference") is None
    assert scheduled == [True]
