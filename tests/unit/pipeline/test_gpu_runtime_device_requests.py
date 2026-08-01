from __future__ import annotations

from unittest.mock import patch

from aigateway_core.pipelines.generation._common.config import (
    TokenCompressorConfig,
)
from aigateway_core.pipelines.generation.token._token_compressor_impl import (
    TokenCompressorStrategy,
)
from aigateway_core.pipelines.understanding.rag.rag_retriever_plugin import (
    RAGRetrieverPlugin,
)
from aigateway_core.shared.integration_configs import CLIPConfig, RAGRetrieverConfig


def test_token_compressor_keeps_auto_as_configured_request() -> None:
    strategy = TokenCompressorStrategy(
        TokenCompressorConfig(), CLIPConfig(device="auto")
    )
    strategy.set_runtime_device("cuda:1")

    assert strategy.gpu_device_request == "auto"
    assert strategy._device == "cuda:1"


def test_code_rag_query_encoder_uses_runtime_assigned_device() -> None:
    plugin = RAGRetrieverPlugin(
        RAGRetrieverConfig(
            embedding_backend="local",
            embedding_device="auto",
            rerank_backend="remote",
        )
    )
    plugin.set_runtime_device("cuda:1")

    with patch(
        "aigateway_core.pipelines.understanding.code_rag.embedding_router.encode_texts",
        return_value=[[0.1, 0.2]],
    ) as encode:
        assert plugin._encode_query("hello") == [0.1, 0.2]

    assert encode.call_args.kwargs["device"] == "cuda:1"
