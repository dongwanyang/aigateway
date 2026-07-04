"""
Tests for GenerationOptimizationConfigWatcher — 配置热重载监视器
================================================================

验证:
- 注册 ConfigManager 的 on_reload 回调
- 有效配置变更时原子交换
- 无效配置（类型/范围错误）保留旧值并记录 ERROR
- 非字典类型的 generation_optimization 节被拒绝
- 缺失 generation_optimization 节时保留当前配置
- on_change 回调正确触发
- 线程安全（并发读写配置）

需求: 6.4, 6.5
"""

import logging
import os
import tempfile
import threading
import time

import pytest
import yaml

# Ensure correct import path
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.config import ConfigManager
from aigateway_core.generation_optimization.config import (
    GenerationOptimizationConfig,
    GenerationOptimizationConfigWatcher,
)
from aigateway_core.generation_optimization.exceptions import (
    ConfigValidationError,
    DraftWorkflowError,
    FeatureCacheError,
    GenerationOptimizationError,
    ModelRoutingError,
    PromptOptimizationError,
    TemplateValidationError,
    TokenCompressionError,
)


@pytest.fixture
def config_file():
    """Create a temporary config file with generation_optimization section."""
    config_data = {
        "server": {"host": "0.0.0.0", "port": 8000},
        "generation_optimization": {
            "enabled": True,
            "ai_director": {
                "enabled": True,
                "timeout_seconds": 10.0,
                "max_prompt_length": 2000,
                "min_prompt_length": 10,
                "rewrite_model": "gpt-4o-mini",
            },
            "model_router": {
                "enabled": True,
                "default_model": "agnes-2.0-flash",
                "model_capabilities": {"agnes-2.0-flash": 50, "agnes-video-v2.0": 90},
                "model_modalities": {"agnes-2.0-flash": ["llm"], "agnes-video-v2.0": ["generative"]},
            },
            "token_compressor": {
                "enabled": True,
                "target_compression_ratio": 0.5,
                "max_vector_dimensions": 512,
            },
            "feature_cache": {
                "ttl_days": 30,
                "lookup_timeout_ms": 500,
            },
            "cost_tracking": {
                "assumed_retry_rate": 0.3,
            },
            "draft_workflow": {
                "max_regeneration_attempts": 5,
                "target_fps": 60,
            },
            "prompt_templates": {
                "default_page_size": 20,
            },
        },
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(config_data, f)
        path = f.name
    yield path
    os.unlink(path)


@pytest.fixture
def config_manager(config_file):
    """Create a ConfigManager instance from the temp config file."""
    return ConfigManager(config_file)


@pytest.fixture
def watcher(config_manager):
    """Create a GenerationOptimizationConfigWatcher instance."""
    return GenerationOptimizationConfigWatcher(config_manager)


class TestWatcherInitialization:
    """Test watcher initialization and registration."""

    def test_registers_with_config_manager(self, config_manager):
        """Watcher should register its callback with ConfigManager.on_reload()."""
        initial_callbacks = len(config_manager._reload_callbacks)
        watcher = GenerationOptimizationConfigWatcher(config_manager)
        assert len(config_manager._reload_callbacks) == initial_callbacks + 1

    def test_loads_initial_config(self, watcher):
        """Watcher should load config from ConfigManager on initialization."""
        cfg = watcher.config
        assert cfg.enabled is True
        assert cfg.ai_director.timeout_seconds == 10.0
        assert cfg.ai_director.max_prompt_length == 2000
        assert cfg.model_router.default_model == "agnes-2.0-flash"

    def test_uses_provided_initial_config(self, config_manager):
        """Watcher should use provided initial_config if given."""
        custom = GenerationOptimizationConfig(enabled=False)
        watcher = GenerationOptimizationConfigWatcher(config_manager, initial_config=custom)
        assert watcher.config.enabled is False


class TestHotReloadValidConfig:
    """Test hot reload with valid config changes."""

    def test_valid_config_applied(self, watcher):
        """Valid config changes should be applied atomically."""
        new_config = {
            "generation_optimization": {
                "enabled": False,
                "ai_director": {
                    "timeout_seconds": 20.0,
                    "max_prompt_length": 4000,
                },
            },
        }
        watcher._on_config_reload(new_config)
        cfg = watcher.config
        assert cfg.enabled is False
        assert cfg.ai_director.timeout_seconds == 20.0
        assert cfg.ai_director.max_prompt_length == 4000

    def test_partial_config_update(self, watcher):
        """Updating one section should not affect others' current values."""
        new_config = {
            "generation_optimization": {
                "enabled": True,
                "ai_director": {"timeout_seconds": 15.0},
                "model_router": {"default_model": "gpt-4"},
            },
        }
        watcher._on_config_reload(new_config)
        cfg = watcher.config
        assert cfg.ai_director.timeout_seconds == 15.0
        assert cfg.model_router.default_model == "gpt-4"
        # token_compressor should retain old value since previous had it
        assert cfg.token_compressor.target_compression_ratio == 0.5


class TestHotReloadInvalidConfig:
    """Test hot reload with invalid config — should retain old values."""

    def test_invalid_type_retains_old(self, watcher):
        """Invalid type (negative timeout) should retain previous valid value."""
        new_config = {
            "generation_optimization": {
                "ai_director": {
                    "timeout_seconds": -5.0,  # Invalid: out of range [1.0, 120.0]
                },
            },
        }
        watcher._on_config_reload(new_config)
        cfg = watcher.config
        # Should retain old value
        assert cfg.ai_director.timeout_seconds == 10.0

    def test_invalid_range_retains_old(self, watcher):
        """Out-of-range compression ratio should retain previous valid value."""
        new_config = {
            "generation_optimization": {
                "token_compressor": {
                    "target_compression_ratio": 2.0,  # Invalid: range is 0.2-0.9
                },
            },
        }
        watcher._on_config_reload(new_config)
        cfg = watcher.config
        assert cfg.token_compressor.target_compression_ratio == 0.5

    def test_mixed_valid_invalid(self, watcher):
        """Valid fields should update while invalid fields retain old values."""
        new_config = {
            "generation_optimization": {
                "ai_director": {
                    "timeout_seconds": -1.0,  # Invalid
                    "max_prompt_length": 3000,  # Valid
                },
            },
        }
        watcher._on_config_reload(new_config)
        cfg = watcher.config
        assert cfg.ai_director.timeout_seconds == 10.0  # Retained
        assert cfg.ai_director.max_prompt_length == 3000  # Updated

    def test_non_dict_section_rejected(self, watcher):
        """Non-dict generation_optimization section should be rejected entirely."""
        watcher._on_config_reload({"generation_optimization": "invalid_string"})
        cfg = watcher.config
        # All values should be unchanged
        assert cfg.enabled is True
        assert cfg.ai_director.timeout_seconds == 10.0

    def test_missing_section_preserves_current(self, watcher):
        """Missing generation_optimization key should preserve current config."""
        watcher._on_config_reload({"server": {"port": 9000}})
        cfg = watcher.config
        assert cfg.enabled is True
        assert cfg.ai_director.timeout_seconds == 10.0

    def test_invalid_model_modality(self, watcher):
        """Invalid model modality values are stored as-is in config.
        
        Modality validation happens at routing time, not config load time.
        The config layer stores whatever dict values are provided.
        """
        new_config = {
            "generation_optimization": {
                "model_router": {
                    "model_modalities": {"bad-model": "invalid_modality"},
                },
            },
        }
        watcher._on_config_reload(new_config)
        cfg = watcher.config
        # Config stores the value as-is; routing strategy validates at use time
        assert cfg.model_router.model_modalities == {"bad-model": "invalid_modality"}


class TestOnChangeCallback:
    """Test the on_change callback mechanism."""

    def test_callback_invoked_on_valid_reload(self, watcher):
        """on_change callback should be invoked when config changes."""
        results = []
        watcher.on_change(lambda cfg: results.append(cfg.enabled))

        watcher._on_config_reload({
            "generation_optimization": {"enabled": False},
        })

        assert len(results) == 1
        assert results[0] is False

    def test_callback_receives_new_config(self, watcher):
        """Callback should receive the updated config instance."""
        configs = []
        watcher.on_change(lambda cfg: configs.append(cfg))

        watcher._on_config_reload({
            "generation_optimization": {
                "cost_tracking": {"assumed_retry_rate": 0.7},
            },
        })

        assert len(configs) == 1
        assert configs[0].cost_tracking.assumed_retry_rate == 0.7

    def test_callback_error_does_not_break_watcher(self, watcher, caplog):
        """Exception in callback should be logged but not break the watcher."""
        def bad_callback(cfg):
            raise RuntimeError("callback error")

        watcher.on_change(bad_callback)

        # Should not raise
        watcher._on_config_reload({
            "generation_optimization": {"enabled": False},
        })

        # Config should still be updated
        assert watcher.config.enabled is False

    def test_multiple_callbacks(self, watcher):
        """Multiple callbacks should all be invoked."""
        results_1 = []
        results_2 = []
        watcher.on_change(lambda cfg: results_1.append(True))
        watcher.on_change(lambda cfg: results_2.append(True))

        watcher._on_config_reload({
            "generation_optimization": {"enabled": True},
        })

        assert len(results_1) == 1
        assert len(results_2) == 1


class TestThreadSafety:
    """Test thread safety of the watcher."""

    def test_concurrent_reads_during_reload(self, watcher):
        """Concurrent reads should not fail during a config reload."""
        errors = []

        def reader():
            for _ in range(100):
                try:
                    cfg = watcher.config
                    _ = cfg.enabled
                    _ = cfg.ai_director.timeout_seconds
                except Exception as e:
                    errors.append(e)

        def writer():
            for i in range(50):
                watcher._on_config_reload({
                    "generation_optimization": {
                        "enabled": i % 2 == 0,
                        "ai_director": {"timeout_seconds": float(5 + i % 10)},
                    },
                })

        threads = [
            threading.Thread(target=reader),
            threading.Thread(target=reader),
            threading.Thread(target=writer),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


class TestExceptionClasses:
    """Test all exception classes are properly defined."""

    def test_all_exceptions_exist(self):
        """All required exception classes should be importable."""
        assert issubclass(PromptOptimizationError, GenerationOptimizationError)
        assert issubclass(ModelRoutingError, GenerationOptimizationError)
        assert issubclass(TokenCompressionError, GenerationOptimizationError)
        assert issubclass(DraftWorkflowError, GenerationOptimizationError)
        assert issubclass(FeatureCacheError, GenerationOptimizationError)
        assert issubclass(TemplateValidationError, GenerationOptimizationError)
        assert issubclass(ConfigValidationError, GenerationOptimizationError)

    def test_base_exception_inherits_from_exception(self):
        """GenerationOptimizationError should inherit from Exception."""
        assert issubclass(GenerationOptimizationError, Exception)

    def test_exceptions_can_be_raised_and_caught(self):
        """All exception classes should be raisable and catchable by base."""
        for exc_cls in [
            PromptOptimizationError,
            ModelRoutingError,
            TokenCompressionError,
            DraftWorkflowError,
            FeatureCacheError,
            TemplateValidationError,
            ConfigValidationError,
        ]:
            with pytest.raises(GenerationOptimizationError):
                raise exc_cls(f"test {exc_cls.__name__}")

    def test_exception_message(self):
        """Exceptions should preserve their error message."""
        err = ConfigValidationError("invalid port value")
        assert str(err) == "invalid port value"
