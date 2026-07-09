"""Built-in plugin registration helpers.

Moved from root ``pipeline.py`` as part of the 总分总 runtime split.
Registers all classic and generation-optimization plugins into a ``PluginRegistry``.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from aigateway_core.prefix.plugins.classic_plugins import (
    PIIDetectorPlugin,
    PromptCachePlugin,
    PromptCompressPlugin,
    SemanticCachePlugin,
)
from aigateway_core.shared.plugin_registry import PluginRegistry

logger = logging.getLogger(__name__)


def _register_builtin_plugins(registry: PluginRegistry, config_manager: Any = None) -> None:
    """注册所有内置插件到注册表。

    Args:
        registry: PluginRegistry 实例。
        config_manager: 可选的配置管理器，用于读取插件配置。
    """
    import json

    plugins_config = []
    if config_manager is not None:
        plugins_config = config_manager.get("plugins", []) or []

    # 获取集成配置（用于 PromptCompressPlugin 等）
    prompt_compress_kwargs: Dict[str, Any] = {}
    if config_manager is not None:
        try:
            integration_cfgs = config_manager.integration_configs
            prompt_compress_kwargs = {"config": integration_cfgs.prompt_compress}
        except Exception:
            pass  # 回退到默认配置

    plugin_map = {
        "pii_detector": (PIIDetectorPlugin, {"strategy": "sanitize"}),
        "prompt_cache": (PromptCachePlugin, {}),
        "semantic_cache": (SemanticCachePlugin, {}),
        "prompt_compress": (PromptCompressPlugin, prompt_compress_kwargs),
    }

    # 注册 RAGRetrieverPlugin（可选依赖）
    try:
        from aigateway_core.plugins.rag_retriever_plugin import RAGRetrieverPlugin

        rag_config = None
        if config_manager is not None:
            try:
                integration_cfgs = config_manager.integration_configs
                rag_config = integration_cfgs.rag_retriever
            except Exception:
                pass

        rag_kwargs: Dict[str, Any] = {}
        if rag_config is not None:
            rag_kwargs["config"] = rag_config

        rag_enabled = True
        for pcfg in plugins_config:
            if isinstance(pcfg, dict) and pcfg.get("name") == "rag_retriever":
                rag_enabled = pcfg.get("enabled", True)
                break

        if rag_enabled:
            plugin_map["rag_retriever"] = (RAGRetrieverPlugin, rag_kwargs)
    except ImportError:
        logger.debug("RAGRetrieverPlugin 不可用（导入失败）")

    # 注册 ConvCompressorPlugin（可选依赖）
    try:
        from aigateway_core.plugins.conv_compressor_plugin import ConvCompressorPlugin

        conv_config = None
        if config_manager is not None:
            try:
                integration_cfgs = config_manager.integration_configs
                conv_config = integration_cfgs.conv_compressor
            except Exception:
                pass

        conv_kwargs: Dict[str, Any] = {}
        if conv_config is not None:
            conv_kwargs["config"] = conv_config

        conv_enabled = True
        for pcfg in plugins_config:
            if isinstance(pcfg, dict) and pcfg.get("name") == "conv_compressor":
                conv_enabled = pcfg.get("enabled", True)
                break

        if conv_enabled:
            plugin_map["conv_compressor"] = (ConvCompressorPlugin, conv_kwargs)
    except ImportError:
        logger.debug("ConvCompressorPlugin 不可用（导入失败）")

    # 注册 Media Optimization Plugin（V2）
    try:
        from aigateway_core.media.plugin import MediaOptimizationPlugin

        mol_config = {}
        if config_manager is not None:
            mol_config = config_manager.get("media_optimization", {}) or {}

        if mol_config.get("enabled", False):
            plugin_map["media_optimizer"] = (MediaOptimizationPlugin, {"config": mol_config})
    except ImportError:
        logger.debug("Media Optimization Plugin 不可用（导入失败）")

    for name, (plugin_cls, default_config) in plugin_map.items():
        cfg = None
        for pcfg in plugins_config:
            if isinstance(pcfg, dict) and pcfg.get("name") == name:
                cfg = pcfg
                break

        enabled = True
        priority = 0
        depends_on: list[str] = getattr(plugin_cls, "depends_on", [])
        plugin_config: dict = {}

        if cfg:
            enabled = cfg.get("enabled", True)
            priority = cfg.get("priority", 0)
            depends_on = cfg.get("depends_on", depends_on)
            plugin_config = cfg.get("config", {})

        if "config" in default_config:
            merged_config = default_config
        else:
            merged_config = {**default_config, **plugin_config}

        registry.register(
            name=name,
            plugin_class=plugin_cls,
            enabled=enabled,
            depends_on=depends_on,
            priority=priority,
            config=merged_config,
        )

    # 注册 Generation Optimization Plugins（6 个优化插件）
    try:
        from aigateway_core.generation_optimization.plugins import (
            register_generation_optimization_plugins,
        )

        gen_opt_config = {}
        if config_manager is not None:
            gen_opt_config = config_manager.get("generation_optimization", {}) or {}

        if gen_opt_config.get("enabled", True):
            redis_client = None
            try:
                from aigateway_core.shared.redis_client import RedisClientManager

                redis_client = RedisClientManager.get_client()
            except Exception:
                logger.debug("Redis client 不可用，Generation Optimization 插件将使用内存后备")

            register_generation_optimization_plugins(
                registry=registry,
                config_manager=config_manager,
                redis_client=redis_client,
            )
        else:
            logger.info("Generation Optimization Layer 已禁用 (generation_optimization.enabled=false)")
    except ImportError as exc:
        logger.debug("Generation Optimization Plugins 不可用（导入失败）: %s", exc)
