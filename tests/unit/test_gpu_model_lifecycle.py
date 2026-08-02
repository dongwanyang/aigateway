from __future__ import annotations

from types import SimpleNamespace

from aigateway_core.pipelines.generation._common.config import TokenCompressorConfig
from aigateway_core.pipelines.generation.token.token_compressor import (
    TokenCompressorStrategy,
)
from aigateway_core.pipelines.understanding.compression.plugin import (
    PromptCompressPlugin,
)
from aigateway_core.pipelines.understanding.rag.rag_retriever_plugin import (
    RAGRetrieverPlugin,
)


class _MovableModel:
    def __init__(self) -> None:
        self.moves: list[str] = []

    def to(self, device: str) -> _MovableModel:
        self.moves.append(device)
        return self


def test_clip_busy_release_and_lazy_reload_state() -> None:
    strategy = TokenCompressorStrategy(TokenCompressorConfig())
    model = _MovableModel()
    strategy._clip_model = model
    strategy._clip_processor = object()
    strategy._clip_loaded = True
    strategy._clip_active = 1
    assert strategy.release_if_idle() == {"released": False, "busy": True}
    strategy._clip_active = 0
    assert strategy.release_if_idle() == {"released": True, "busy": False}
    assert model.moves == ["cpu"]
    assert strategy._clip_loaded is False


def test_prompt_compressor_busy_release_and_lazy_reload_state() -> None:
    plugin = PromptCompressPlugin()
    plugin._compressor = object()
    plugin._initialized = True
    plugin._is_available = True
    plugin._active = 1
    assert plugin.release_if_idle() == {"released": False, "busy": True}
    plugin._active = 0
    assert plugin.release_if_idle() == {"released": True, "busy": False}
    assert plugin._compressor is None
    assert plugin._initialized is False


def test_rag_embedding_busy_release_and_lazy_reload_state() -> None:
    plugin = RAGRetrieverPlugin()
    model = _MovableModel()
    plugin._index = SimpleNamespace(
        _embed_model=SimpleNamespace(_model=model)
    )
    plugin._is_available = True
    plugin._active_inference = 1
    assert plugin.release_if_idle() == {"released": False, "busy": True}
    plugin._active_inference = 0
    assert plugin.release_if_idle() == {"released": True, "busy": False}
    assert model.moves == ["cpu"]
    assert plugin._evicted is True
