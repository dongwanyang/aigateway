from __future__ import annotations

from pathlib import Path

import pytest
from aigateway_core.pipelines.understanding.rag.rag_retriever_plugin import (
    RAGRetrieverPlugin,
)
from aigateway_core.shared.integration_configs import RAGRetrieverConfig
from aigateway_core.shared.strict_config_validation import (
    validate_component_config_strict,
)


class _RAGConfig(RAGRetrieverConfig):
    pass


def test_rag_cpu_embedding_is_not_overridden_by_gpu_reranker() -> None:
    plugin = RAGRetrieverPlugin(
        _RAGConfig(
            embedding_backend="local",
            embedding_device="cpu",
            rerank_enabled=True,
            rerank_backend="local",
            rerank_device="cuda",
        )
    )

    assert plugin.gpu_device_request == "cuda"
    plugin.set_runtime_device("cuda:1")

    assert plugin._runtime_embedding_device == "cpu"
    assert plugin._runtime_rerank_device == "cuda:1"


def test_rag_disabled_reranker_does_not_request_its_gpu() -> None:
    plugin = RAGRetrieverPlugin(
        _RAGConfig(
            embedding_backend="local",
            embedding_device="cpu",
            rerank_enabled=False,
            rerank_backend="local",
            rerank_device="cuda",
        )
    )

    assert plugin.gpu_device_request == "cpu"


def test_rag_rejects_two_distinct_explicit_gpu_devices() -> None:
    plugin = RAGRetrieverPlugin(
        _RAGConfig(
            embedding_backend="local",
            embedding_device="cuda:0",
            rerank_enabled=True,
            rerank_backend="local",
            rerank_device="cuda:1",
        )
    )

    with pytest.raises(RuntimeError, match="multiple_gpu"):
        _ = plugin.gpu_device_request


def test_privileged_topology_installer_is_not_shipped() -> None:
    root = Path(__file__).resolve().parents[2]
    assert not (root / "scripts" / "install-gpu-topology-controller.sh").exists()
    quickstart = (root / "scripts" / "quickstart.sh").read_text(encoding="utf-8")
    assert "install-gpu-topology-controller.sh" not in quickstart
    template = (root / "config.yaml.template").read_text(encoding="utf-8")
    assert "topology_auto_apply: false" in template


def test_strict_config_rejects_rag_split_across_explicit_gpus() -> None:
    issues = validate_component_config_strict(
        {
            "plugins": [
                {
                    "name": "rag_retriever",
                    "config": {
                        "embedding_backend": "local",
                        "embedding_device": "cuda:0",
                        "rerank_enabled": True,
                        "rerank_backend": "local",
                        "rerank_device": "cuda:1",
                    },
                }
            ]
        },
        apply_specific_env=False,
    )
    assert any("different explicit accelerator devices" in issue["message"] for issue in issues)


def test_strict_config_allows_cpu_embedding_with_gpu_reranker() -> None:
    issues = validate_component_config_strict(
        {
            "plugins": [
                {
                    "name": "rag_retriever",
                    "config": {
                        "embedding_backend": "local",
                        "embedding_device": "cpu",
                        "rerank_enabled": True,
                        "rerank_backend": "local",
                        "rerank_device": "cuda:1",
                    },
                }
            ]
        },
        apply_specific_env=False,
    )
    assert not issues
