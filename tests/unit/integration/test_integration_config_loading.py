"""
Unit tests for integration config loading — parse_integration_configs().

Validates:
- YAML extraction from all 7 config paths
- Environment variable override with AI_GATEWAY_ prefix
- Type validation and range checking
- Invalid values retain previous config
- Default values match spec
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.shared.config import (
    IntegrationConfigs,
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
    """Test that empty/missing config produces correct defaults."""

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
        assert pc.model_name == "microsoft/llmlingua-2-bert-base-multilingual-cased-meetingbank"
        assert pc.target_token == -1
        assert pc.force_tokens == []
        assert pc.device == "cpu"

    def test_default_clip_values(self):
        result = parse_integration_configs({})
        c = result.clip
        assert c.model_name == "openai/clip-vit-large-patch14"
        assert c.device == "cpu"
        assert c.batch_size == 1

    def test_default_comfyui_values(self):
        result = parse_integration_configs({})
        c = result.comfyui
        assert c.server_url == "http://localhost:8188"
        assert c.connect_timeout == 10
        assert c.execution_timeout == 300
        assert c.ws_reconnect_attempts == 3

    def test_default_rag_retriever_values(self):
        result = parse_integration_configs({})
        r = result.rag_retriever
        assert r.enabled is True
        assert r.top_k == 5
        assert r.similarity_threshold == 0.7
        assert r.rerank_enabled is False
        assert r.chunk_size == 512
        assert r.chunk_overlap == 64

    def test_default_conv_compressor_values(self):
        result = parse_integration_configs({})
        c = result.conv_compressor
        assert c.enabled is True
        assert c.max_history == 20
        # 默认走 gateway 自身而非 OpenAI，避免容器强依赖 OPENAI_API_KEY
        assert c.summary_model == "agnes-2.0-flash"
        assert c.max_token_limit == 4000
        assert c.summary_interval == 5
        assert c.api_base == "http://localhost:8000/v1"
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


class TestTypeValidation:
    """Test type checking rejects invalid types and retains old config."""

    def test_string_for_int_field_retains_old(self):
        previous = parse_integration_configs(
            {"plugins": [{"name": "rag_retriever", "config": {"top_k": 3}}]}
        )
        config_bad = {"plugins": [{"name": "rag_retriever", "config": {"top_k": "not_int"}}]}
        result = parse_integration_configs(config_bad, previous)
        assert result.rag_retriever.top_k == 3

    def test_string_for_float_field_retains_old(self):
        previous = parse_integration_configs(
            {"plugins": [{"name": "prompt_compress", "config": {"compression_ratio": 0.4}}]}
        )
        config_bad = {"plugins": [{"name": "prompt_compress", "config": {"compression_ratio": "high"}}]}
        result = parse_integration_configs(config_bad, previous)
        assert result.prompt_compress.compression_ratio == 0.4

    def test_int_for_bool_field_retains_old(self):
        previous = parse_integration_configs(
            {"media_optimization": {"image": {"paddleocr": {"use_angle_cls": True}}}}
        )
        config_bad = {"media_optimization": {"image": {"paddleocr": {"use_angle_cls": 42}}}}
        result = parse_integration_configs(config_bad, previous)
        assert result.paddleocr.use_angle_cls is True

    def test_bool_for_int_field_retains_old(self):
        """bool is subclass of int in Python, but should not be accepted for int fields."""
        previous = parse_integration_configs(
            {"plugins": [{"name": "conv_compressor", "config": {"max_history": 15}}]}
        )
        config_bad = {"plugins": [{"name": "conv_compressor", "config": {"max_history": True}}]}
        result = parse_integration_configs(config_bad, previous)
        assert result.conv_compressor.max_history == 15


class TestRangeValidation:
    """Test range/constraint validation."""

    def test_compression_ratio_above_max_retains_old(self):
        previous = parse_integration_configs(
            {"plugins": [{"name": "prompt_compress", "config": {"compression_ratio": 0.5}}]}
        )
        config_bad = {"plugins": [{"name": "prompt_compress", "config": {"compression_ratio": 1.5}}]}
        result = parse_integration_configs(config_bad, previous)
        assert result.prompt_compress.compression_ratio == 0.5

    def test_compression_ratio_below_min_retains_old(self):
        previous = parse_integration_configs(
            {"plugins": [{"name": "prompt_compress", "config": {"compression_ratio": 0.5}}]}
        )
        config_bad = {"plugins": [{"name": "prompt_compress", "config": {"compression_ratio": -0.1}}]}
        result = parse_integration_configs(config_bad, previous)
        assert result.prompt_compress.compression_ratio == 0.5

    def test_top_k_below_min_retains_old(self):
        previous = parse_integration_configs(
            {"plugins": [{"name": "rag_retriever", "config": {"top_k": 5}}]}
        )
        config_bad = {"plugins": [{"name": "rag_retriever", "config": {"top_k": 0}}]}
        result = parse_integration_configs(config_bad, previous)
        assert result.rag_retriever.top_k == 5

    def test_batch_size_below_min_retains_old(self):
        previous = parse_integration_configs(
            {"generation_optimization": {"token_compressor": {"clip": {"batch_size": 2}}}}
        )
        config_bad = {"generation_optimization": {"token_compressor": {"clip": {"batch_size": 0}}}}
        result = parse_integration_configs(config_bad, previous)
        assert result.clip.batch_size == 2

    def test_strategy_invalid_choice_retains_old(self):
        previous = parse_integration_configs(
            {"media_optimization": {"document": {"unstructured": {"strategy": "fast"}}}}
        )
        config_bad = {"media_optimization": {"document": {"unstructured": {"strategy": "invalid"}}}}
        result = parse_integration_configs(config_bad, previous)
        assert result.unstructured.strategy == "fast"

    def test_similarity_threshold_above_max_retains_old(self):
        previous = parse_integration_configs(
            {"plugins": [{"name": "rag_retriever", "config": {"similarity_threshold": 0.7}}]}
        )
        config_bad = {"plugins": [{"name": "rag_retriever", "config": {"similarity_threshold": 1.5}}]}
        result = parse_integration_configs(config_bad, previous)
        assert result.rag_retriever.similarity_threshold == 0.7

    def test_valid_boundary_values_accepted(self):
        config = {
            "plugins": [
                {"name": "prompt_compress", "config": {"compression_ratio": 0.0}},
                {"name": "rag_retriever", "config": {"similarity_threshold": 1.0, "top_k": 1}},
            ]
        }
        result = parse_integration_configs(config)
        assert result.prompt_compress.compression_ratio == 0.0
        assert result.rag_retriever.similarity_threshold == 1.0
        assert result.rag_retriever.top_k == 1


class TestPreviousFallback:
    """Test that invalid values fall back to previous config, not defaults."""

    def test_invalid_value_uses_previous_not_default(self):
        """When previous has non-default value and new config is invalid,
        should retain previous value, not fall back to class default."""
        previous = parse_integration_configs(
            {"plugins": [{"name": "prompt_compress", "config": {"compression_ratio": 0.3}}]}
        )
        assert previous.prompt_compress.compression_ratio == 0.3  # not 0.5 (default)

        config_bad = {"plugins": [{"name": "prompt_compress", "config": {"compression_ratio": 2.0}}]}
        result = parse_integration_configs(config_bad, previous)
        # Should be 0.3 (previous), not 0.5 (default)
        assert result.prompt_compress.compression_ratio == 0.3

    def test_no_previous_invalid_value_uses_default(self):
        """When no previous and new config is invalid, field uses dataclass default."""
        config_bad = {"plugins": [{"name": "prompt_compress", "config": {"compression_ratio": 2.0}}]}
        result = parse_integration_configs(config_bad, None)
        # Should fall back to default 0.5
        assert result.prompt_compress.compression_ratio == 0.5
