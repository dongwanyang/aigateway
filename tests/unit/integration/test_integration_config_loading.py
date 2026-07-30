"""
Unit tests for integration config loading — parse_integration_configs().

Validates:
- YAML extraction from all 7 config paths
- Environment variable override with AI_GATEWAY_ prefix
- Type validation and range checking
- Invalid values retain previous config
- Deployment values remain unconfigured until YAML/env supplies them
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.shared.config import (
    parse_integration_configs,
)
from aigateway_core.shared.integration_configs import (
    CLIPConfig,
    ComfyUIConfig,
    ConvCompressorConfig,
    PaddleOCRConfig,
    PromptCompressConfig,
    RAGRetrieverConfig,
    UnstructuredConfig,
)


class TestParseIntegrationConfigsDefaults:
    """Test that empty/missing config produces safe generic defaults."""

    def test_empty_config_returns_all_defaults(self):
        result = parse_integration_configs({})
        assert result.prompt_compress == PromptCompressConfig()
        assert result.clip == CLIPConfig()
        assert result.comfyui == ComfyUIConfig()
        assert result.rag_retriever == RAGRetrieverConfig()
        assert result.conv_compressor == ConvCompressorConfig()
        assert result.paddleocr == PaddleOCRConfig()
        assert result.unstructured == UnstructuredConfig()

    def test_default_prompt_compress_values(self):
        result = parse_integration_configs({})
        pc = result.prompt_compress
        assert pc.enabled is True
        assert pc.compression_ratio == 0.5
        assert pc.model_name == ""
        assert pc.target_token == -1
        assert pc.force_tokens == []
        assert pc.device == "cpu"

    def test_default_clip_values(self):
        result = parse_integration_configs({})
        c = result.clip
        assert c.model_name == ""
        assert c.device == "cpu"
        assert c.batch_size == 1

    def test_default_comfyui_values(self):
        result = parse_integration_configs({})
        c = result.comfyui
        assert c.server_url == ""
        assert c.public_url == ""
        assert c.connect_timeout == 10
        assert c.execution_timeout == 1200
        assert c.qwen_image_draft_steps == 12
        assert c.qwen_image_max_draft_edge == 768
        assert c.ws_reconnect_attempts == 3
        assert c.required is True
        assert c.workflow_version == ""
        assert c.checkpoint_name == ""
        assert c.allowed_checkpoints == []
        assert c.models_path == ""
        assert c.output_path == ""
        assert c.workflow_path == ""
        assert c.upscale_model == ""
        assert c.allowed_upscale_models == []
        assert c.max_concurrency == 1
        assert c.min_free_gb == 30.0
        assert c.model_budget_gb == 80.0
        assert c.output_budget_gb == 10.0
        assert c.video_enabled is True
        assert c.video_workflow_version == ""
        assert c.video_width == 512
        assert c.video_height == 288
        assert c.video_frames == 17
        assert c.video_execution_timeout == 1200

    def test_default_rag_retriever_values(self):
        result = parse_integration_configs({})
        r = result.rag_retriever
        assert r.enabled is True
        assert r.top_k == 5
        assert r.similarity_threshold == 0.7
        assert r.rerank_enabled is False
        assert r.rerank_model == ""
        assert r.chunk_size == 512
        assert r.chunk_overlap == 64
        assert r.collection_name == ""
        assert r.embedding_model == ""
        assert r.code_graph_db_dir == ""

    def test_default_conv_compressor_values(self):
        result = parse_integration_configs({})
        c = result.conv_compressor
        assert c.enabled is True
        assert c.max_history == 20
        assert c.summary_model == ""
        assert c.max_token_limit == 4000
        assert c.summary_interval == 5
        assert c.api_base == ""
        assert c.api_key is None

    def test_default_paddleocr_values(self):
        result = parse_integration_configs({})
        p = result.paddleocr
        assert p.lang == "ch"
        assert p.use_angle_cls is True
        assert p.det_model_dir is None
        assert p.rec_model_dir is None

    def test_default_unstructured_values(self):
        result = parse_integration_configs({})
        u = result.unstructured
        assert u.strategy == "auto"
        assert u.languages == ["chi_sim", "eng"]
        assert u.extract_images is False


class TestYAMLExtraction:
    """Test YAML config section extraction for each integration."""

    def test_plugin_config_extraction_prompt_compress(self):
        config = {
            "plugins": [
                {"name": "prompt_compress", "config": {"compression_ratio": 0.3, "device": "cuda"}}
            ]
        }
        result = parse_integration_configs(config)
        assert result.prompt_compress.compression_ratio == 0.3
        assert result.prompt_compress.device == "cuda"

    def test_plugin_config_extraction_rag_retriever(self):
        config = {
            "plugins": [
                {"name": "rag_retriever", "config": {"top_k": 10, "similarity_threshold": 0.8}}
            ]
        }
        result = parse_integration_configs(config)
        assert result.rag_retriever.top_k == 10
        assert result.rag_retriever.similarity_threshold == 0.8

    def test_plugin_config_extraction_conv_compressor(self):
        config = {
            "plugins": [
                {"name": "conv_compressor", "config": {"max_history": 50, "summary_model": "gpt-4o"}}
            ]
        }
        result = parse_integration_configs(config)
        assert result.conv_compressor.max_history == 50
        assert result.conv_compressor.summary_model == "gpt-4o"

    def test_generation_optimization_clip(self):
        config = {
            "generation_optimization": {
                "token_compressor": {
                    "clip": {"model_name": "custom/clip", "batch_size": 4}
                }
            }
        }
        result = parse_integration_configs(config)
        assert result.clip.model_name == "custom/clip"
        assert result.clip.batch_size == 4

    def test_generation_optimization_comfyui(self):
        config = {
            "generation_optimization": {
                "draft_workflow": {
                    "comfyui": {"server_url": "http://remote:9000", "execution_timeout": 600}
                }
            }
        }
        result = parse_integration_configs(config)
        assert result.comfyui.server_url == "http://remote:9000"
        assert result.comfyui.execution_timeout == 600

    def test_media_optimization_paddleocr(self):
        config = {
            "media_optimization": {
                "image": {
                    "paddleocr": {"lang": "en", "use_angle_cls": False}
                }
            }
        }
        result = parse_integration_configs(config)
        assert result.paddleocr.lang == "en"
        assert result.paddleocr.use_angle_cls is False

    def test_media_optimization_unstructured(self):
        config = {
            "media_optimization": {
                "document": {
                    "unstructured": {"strategy": "hi_res", "extract_images": True}
                }
            }
        }
        result = parse_integration_configs(config)
        assert result.unstructured.strategy == "hi_res"
        assert result.unstructured.extract_images is True

    def test_missing_plugin_uses_defaults(self):
        config = {"plugins": [{"name": "other_plugin", "config": {}}]}
        result = parse_integration_configs(config)
        assert result.prompt_compress == PromptCompressConfig()


class TestEnvironmentVariableOverride:
    """Test AI_GATEWAY_ prefix environment variable overrides."""

    def test_env_overrides_yaml_value(self, monkeypatch):
        monkeypatch.setenv("AI_GATEWAY_PROMPT_COMPRESS_COMPRESSION_RATIO", "0.8")
        config = {
            "plugins": [
                {"name": "prompt_compress", "config": {"compression_ratio": 0.3}}
            ]
        }
        result = parse_integration_configs(config)
        assert result.prompt_compress.compression_ratio == 0.8

    def test_env_overrides_default_value(self, monkeypatch):
        monkeypatch.setenv("AI_GATEWAY_CLIP_BATCH_SIZE", "16")
        result = parse_integration_configs({})
        assert result.clip.batch_size == 16

    def test_env_override_bool(self, monkeypatch):
        monkeypatch.setenv("AI_GATEWAY_RAG_RETRIEVER_RERANK_ENABLED", "true")
        result = parse_integration_configs({})
        assert result.rag_retriever.rerank_enabled is True

    def test_env_override_string(self, monkeypatch):
        monkeypatch.setenv("AI_GATEWAY_COMFYUI_SERVER_URL", "http://custom:7777")
        result = parse_integration_configs({})
        assert result.comfyui.server_url == "http://custom:7777"

    def test_env_override_float(self, monkeypatch):
        monkeypatch.setenv("AI_GATEWAY_RAG_RETRIEVER_SIMILARITY_THRESHOLD", "0.85")
        result = parse_integration_configs({})
        assert result.rag_retriever.similarity_threshold == 0.85

    def test_env_override_json_list(self, monkeypatch):
        monkeypatch.setenv("AI_GATEWAY_UNSTRUCTURED_LANGUAGES", '["eng", "fra"]')
        result = parse_integration_configs({})
        assert result.unstructured.languages == ["eng", "fra"]

    def test_env_override_paddleocr(self, monkeypatch):
        monkeypatch.setenv("AI_GATEWAY_PADDLEOCR_LANG", "en")
        result = parse_integration_configs({})
        assert result.paddleocr.lang == "en"
