from __future__ import annotations

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
