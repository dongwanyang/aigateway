"""Unit tests for config-backed integration dataclasses."""

from __future__ import annotations

from aigateway_core.shared.config import parse_integration_configs
from aigateway_core.shared.integration_configs import (
    CLIPConfig,
    ComfyUIConfig,
    ConvCompressorConfig,
    PaddleOCRConfig,
    PromptCompressConfig,
    RAGRetrieverConfig,
    UnstructuredConfig,
)


class TestSafeUnconfiguredValues:
    def test_empty_config_returns_all_config_objects(self):
        result = parse_integration_configs({})
        assert result.prompt_compress == PromptCompressConfig()
        assert result.clip == CLIPConfig()
        assert result.comfyui == ComfyUIConfig()
        assert result.rag_retriever == RAGRetrieverConfig()
        assert result.conv_compressor == ConvCompressorConfig()
        assert result.paddleocr == PaddleOCRConfig()
        assert result.unstructured == UnstructuredConfig()

    def test_empty_config_does_not_guess_reported_deployment_values(self):
        result = parse_integration_configs({})
        assert result.prompt_compress.model_name == ""
        assert result.clip.model_name == ""

        comfy = result.comfyui
        assert comfy.server_url == ""
        assert comfy.public_url == ""
        assert comfy.workflow_version == ""
        assert comfy.checkpoint_name == ""
        assert comfy.allowed_checkpoints == []
        assert comfy.models_path == ""
        assert comfy.output_path == ""
        assert comfy.workflow_path == ""
        assert comfy.upscale_model == ""
        assert comfy.allowed_upscale_models == []

        rag = result.rag_retriever
        assert rag.rerank_model == ""
        assert rag.collection_name == ""
        assert rag.embedding_model == ""
        assert rag.code_graph_db_dir == ""

        conv = result.conv_compressor
        assert conv.summary_model == ""
        assert conv.api_base == ""

    def test_generic_algorithm_defaults_remain_available(self):
        result = parse_integration_configs({})
        assert result.prompt_compress.compression_ratio == 0.5
        assert result.clip.batch_size == 1
        assert result.comfyui.connect_timeout == 10
        assert result.comfyui.execution_timeout == 1200
        assert result.rag_retriever.top_k == 5
        assert result.rag_retriever.similarity_threshold == 0.7
        assert result.conv_compressor.max_history == 20
        assert result.paddleocr.lang == "ch"
        assert result.unstructured.strategy == "auto"


class TestYamlExtraction:
    def test_plugin_config_extraction_prompt_compress(self):
        result = parse_integration_configs({
            "plugins": [{
                "name": "prompt_compress",
                "config": {
                    "compression_ratio": 0.3,
                    "model_name": "org/compressor",
                    "device": "cuda",
                },
            }]
        })
        assert result.prompt_compress.compression_ratio == 0.3
        assert result.prompt_compress.model_name == "org/compressor"
        assert result.prompt_compress.device == "cuda"

    def test_generation_optimization_clip(self):
        result = parse_integration_configs({
            "generation_optimization": {
                "token_compressor": {
                    "clip": {
                        "model_name": "custom/clip",
                        "device": "cpu",
                        "batch_size": 4,
                    }
                }
            }
        })
        assert result.clip.model_name == "custom/clip"
        assert result.clip.batch_size == 4

    def test_generation_optimization_comfyui(self):
        result = parse_integration_configs({
            "generation_optimization": {
                "draft_workflow": {
                    "comfyui": {
                        "server_url": "http://remote:9000",
                        "public_url": "https://comfy.example",
                        "execution_timeout": 600,
                        "workflow_version": "custom-v2",
                        "checkpoint_name": "custom.safetensors",
                        "allowed_checkpoints": ["custom.safetensors"],
                        "models_path": "/srv/comfy/models",
                        "output_path": "/srv/comfy/output",
                        "workflow_path": "/srv/comfy/workflows",
                    }
                }
            }
        })
        comfy = result.comfyui
        assert comfy.server_url == "http://remote:9000"
        assert comfy.public_url == "https://comfy.example"
        assert comfy.execution_timeout == 600
        assert comfy.workflow_version == "custom-v2"
        assert comfy.checkpoint_name == "custom.safetensors"
        assert comfy.models_path == "/srv/comfy/models"

    def test_rag_and_conversation_models_are_yaml_values(self):
        result = parse_integration_configs({
            "plugins": [
                {
                    "name": "rag_retriever",
                    "config": {
                        "top_k": 10,
                        "similarity_threshold": 0.8,
                        "rerank_model": "org/reranker",
                        "collection_name": "documents-v2",
                        "embedding_model": "org/embedding",
                        "code_graph_db_dir": "/srv/code-graphs",
                    },
                },
                {
                    "name": "conv_compressor",
                    "config": {
                        "max_history": 50,
                        "summary_model": "summary-model",
                        "api_base": "http://gateway.internal/v1",
                    },
                },
            ]
        })
        assert result.rag_retriever.rerank_model == "org/reranker"
        assert result.rag_retriever.collection_name == "documents-v2"
        assert result.rag_retriever.embedding_model == "org/embedding"
        assert result.rag_retriever.code_graph_db_dir == "/srv/code-graphs"
        assert result.conv_compressor.summary_model == "summary-model"
        assert result.conv_compressor.api_base == "http://gateway.internal/v1"

    def test_media_pipeline_configs(self):
        result = parse_integration_configs({
            "media_optimization": {
                "image": {
                    "paddleocr": {"lang": "en", "use_angle_cls": False}
                },
                "document": {
                    "unstructured": {
                        "strategy": "hi_res",
                        "languages": ["eng"],
                        "extract_images": True,
                    }
                },
            }
        })
        assert result.paddleocr.lang == "en"
        assert result.paddleocr.use_angle_cls is False
        assert result.unstructured.strategy == "hi_res"
        assert result.unstructured.languages == ["eng"]
        assert result.unstructured.extract_images is True


class TestEnvironmentOverrides:
    def test_env_overrides_yaml_value(self, monkeypatch):
        monkeypatch.setenv("AI_GATEWAY_PROMPT_COMPRESS_MODEL_NAME", "env/compressor")
        result = parse_integration_configs({
            "plugins": [{
                "name": "prompt_compress",
                "config": {"model_name": "yaml/compressor"},
            }]
        })
        assert result.prompt_compress.model_name == "env/compressor"

    def test_env_can_supply_missing_deployment_value(self, monkeypatch):
        monkeypatch.setenv("AI_GATEWAY_COMFYUI_SERVER_URL", "http://custom:7777")
        result = parse_integration_configs({})
        assert result.comfyui.server_url == "http://custom:7777"

    def test_env_json_list(self, monkeypatch):
        monkeypatch.setenv("AI_GATEWAY_UNSTRUCTURED_LANGUAGES", '["eng", "fra"]')
        result = parse_integration_configs({})
        assert result.unstructured.languages == ["eng", "fra"]


class TestValidationAndPreviousFallback:
    def test_invalid_type_retains_previous(self):
        previous = parse_integration_configs({
            "plugins": [{"name": "rag_retriever", "config": {"top_k": 3}}]
        })
        result = parse_integration_configs({
            "plugins": [{"name": "rag_retriever", "config": {"top_k": "bad"}}]
        }, previous)
        assert result.rag_retriever.top_k == 3

    def test_invalid_range_retains_previous(self):
        previous = parse_integration_configs({
            "plugins": [{
                "name": "prompt_compress",
                "config": {"compression_ratio": 0.3},
            }]
        })
        result = parse_integration_configs({
            "plugins": [{
                "name": "prompt_compress",
                "config": {"compression_ratio": 2.0},
            }]
        }, previous)
        assert result.prompt_compress.compression_ratio == 0.3

    def test_invalid_without_previous_uses_safe_generic_default(self):
        result = parse_integration_configs({
            "plugins": [{
                "name": "prompt_compress",
                "config": {"compression_ratio": 2.0},
            }]
        })
        assert result.prompt_compress.compression_ratio == 0.5

    def test_valid_boundary_values_are_accepted(self):
        result = parse_integration_configs({
            "plugins": [
                {
                    "name": "prompt_compress",
                    "config": {"compression_ratio": 0.0},
                },
                {
                    "name": "rag_retriever",
                    "config": {"similarity_threshold": 1.0, "top_k": 1},
                },
            ]
        })
        assert result.prompt_compress.compression_ratio == 0.0
        assert result.rag_retriever.similarity_threshold == 1.0
        assert result.rag_retriever.top_k == 1
