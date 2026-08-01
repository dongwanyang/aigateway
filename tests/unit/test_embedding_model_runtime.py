from __future__ import annotations

import threading

import pytest
from fastapi import HTTPException

from aigateway_api import gpu_routes
from aigateway_api.embedding_model_runtime import EmbeddingModelRuntime
from aigateway_api.embedding_model_runtime import embedding_model_runtime


class Model:
    def __init__(self) -> None:
        self.devices: list[str] = []

    def to(self, device: str) -> "Model":
        self.devices.append(device)
        return self


def test_embedding_model_release_refuses_active_inference() -> None:
    runtime = EmbeddingModelRuntime()
    model = Model()

    with runtime.lease(lambda: model) as leased:
        assert leased is model
        assert runtime.active_count == 1
        assert runtime.release_if_idle() == {"released": False, "busy": True}
        assert runtime.cache["model"] is model

    assert runtime.release_if_idle() == {"released": True, "busy": False}
    assert runtime.cache == {}
    assert model.devices == ["cpu"]


def test_invalid_idle_cache_can_be_discarded_safely() -> None:
    runtime = EmbeddingModelRuntime()
    invalid = object()
    runtime.cache["model"] = invalid

    assert runtime.discard_invalid_if_idle(
        lambda model: not callable(getattr(model, "encode", None))
    ) is True
    assert runtime.cache == {}

    valid = type("ValidModel", (), {"encode": lambda self: None})()
    runtime.cache["model"] = valid
    assert runtime.discard_invalid_if_idle(
        lambda model: not callable(getattr(model, "encode", None))
    ) is False
    assert runtime.cache["model"] is valid


def test_invalid_cache_is_not_discarded_during_active_lease() -> None:
    runtime = EmbeddingModelRuntime()
    invalid = object()
    with runtime.lease(lambda: invalid):
        assert runtime.discard_invalid_if_idle(lambda _model: True) is False
        assert runtime.cache["model"] is invalid


def test_new_inference_waits_until_release_finishes() -> None:
    runtime = EmbeddingModelRuntime()
    release_started = threading.Event()
    allow_release = threading.Event()
    lease_acquired = threading.Event()

    class SlowModel(Model):
        def to(self, device: str) -> "SlowModel":
            release_started.set()
            assert allow_release.wait(timeout=2)
            super().to(device)
            return self

    first = SlowModel()
    replacement = Model()
    with runtime.lease(lambda: first):
        pass

    release_result: dict[str, bool] = {}

    def release() -> None:
        release_result.update(runtime.release_if_idle())

    def acquire_replacement() -> None:
        with runtime.lease(lambda: replacement):
            lease_acquired.set()

    release_thread = threading.Thread(target=release)
    release_thread.start()
    assert release_started.wait(timeout=1)

    lease_thread = threading.Thread(target=acquire_replacement)
    lease_thread.start()
    assert not lease_acquired.wait(timeout=0.05)

    allow_release.set()
    release_thread.join(timeout=2)
    lease_thread.join(timeout=2)

    assert not release_thread.is_alive()
    assert not lease_thread.is_alive()
    assert release_result == {"released": True, "busy": False}
    assert lease_acquired.is_set()
    assert first.devices == ["cpu"]
    assert runtime.cache["model"] is replacement


@pytest.mark.asyncio
async def test_gpu_release_reports_gateway_embedding_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        embedding_model_runtime,
        "release_if_idle",
        lambda: {"released": False, "busy": True},
    )

    with pytest.raises(HTTPException) as exc_info:
        await gpu_routes._release_gateway_models()

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["error"]["code"] == "gateway_gpu_busy"
