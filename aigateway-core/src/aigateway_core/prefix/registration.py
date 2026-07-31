"""Built-in plugin registration helpers.

Moved from root ``pipeline.py`` as part of the 总分总 runtime split.
Registers all classic and generation-optimization plugins into a ``PluginRegistry``.
"""
from __future__ import annotations

import logging
from typing import Any

from aigateway_core.pipelines.understanding.compression.plugin import (
    PromptCompressPlugin,
)
from aigateway_core.prefix.cache.plugin import PromptCachePlugin, SemanticCachePlugin
from aigateway_core.prefix.pii.plugin import PIIDetectorPlugin
from aigateway_core.shared.plugin_registry import PluginRegistry

logger = logging.getLogger(__name__)


def _register_builtin_plugins(registry: PluginRegistry, config_manager: Any = None) -> None:
    """注册所有内置插件到注册表。"""

    plugins_config = []
    if config_manager is not None:
        plugins_config = config_manager.get("plugins", []) or []

    prompt_compress_kwargs: dict[str, Any] = {}
    if config_manager is not None:
        try:
            integration_cfgs = config_manager.integration_configs
            prompt_compress_kwargs = {"config": integration_cfgs.prompt_compress}
        except Exception:
            logger.warning("PromptCompress 集成配置不可用，使用未配置的安全对象")

    plugin_map = {
        "pii_detector": (PIIDetectorPlugin, {"strategy": "sanitize"}),
        "prompt_cache": (PromptCachePlugin, {}),
        "semantic_cache": (SemanticCachePlugin, {}),
        "prompt_compress": (PromptCompressPlugin, prompt_compress_kwargs),
    }

    try:
        from aigateway_core.pipelines.understanding.rag.configured_rag_retriever import (
            ConfiguredRAGRetrieverPlugin,
        )

        rag_config = None
        if config_manager is not None:
            try:
                integration_cfgs = config_manager.integration_configs
                rag_config = integration_cfgs.rag_retriever

                code_rag_cfg = config_manager.get("code_rag", {}) or {}
                if isinstance(code_rag_cfg, dict):
                    graph_dir = code_rag_cfg.get("graph_db_dir")
                    if isinstance(graph_dir, str) and graph_dir.strip():
                        rag_config.code_graph_db_dir = graph_dir.strip()

                infrastructure = config_manager.get("infrastructure", {}) or {}
                qdrant_cfg = (
                    infrastructure.get("qdrant", {})
                    if isinstance(infrastructure, dict)
                    else {}
                )
                qdrant_url = (
                    qdrant_cfg.get("url") if isinstance(qdrant_cfg, dict) else None
                )
                if isinstance(qdrant_url, str) and qdrant_url.strip():
                    rag_config.qdrant_url = qdrant_url.strip()
            except Exception as exc:
                logger.warning("RAG 集成配置不可用，插件将安全降级: %s", exc)

        rag_kwargs: dict[str, Any] = {}
        if rag_config is not None:
            rag_kwargs["config"] = rag_config

        rag_enabled = True
        for pcfg in plugins_config:
            if isinstance(pcfg, dict) and pcfg.get("name") == "rag_retriever":
                rag_enabled = pcfg.get("enabled", True)
                break

        if rag_enabled:
            plugin_map["rag_retriever"] = (
                ConfiguredRAGRetrieverPlugin,
                rag_kwargs,
            )
    except ImportError:
        logger.debug("RAGRetrieverPlugin 不可用（导入失败）")

    try:
        from aigateway_core.pipelines.understanding.conversation.conv_compressor_plugin import (
            ConvCompressorPlugin,
        )

        conv_config = None
        if config_manager is not None:
            try:
                integration_cfgs = config_manager.integration_configs
                conv_config = integration_cfgs.conv_compressor
            except Exception:
                logger.warning("ConvCompressor 集成配置不可用，使用未配置的安全对象")

        conv_kwargs: dict[str, Any] = {}
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

    try:
        from aigateway_core.prefix.media.plugin import MediaOptimizationPlugin

        mol_config = {}
        if config_manager is not None:
            mol_config = config_manager.get("media_optimization", {}) or {}

        if mol_config.get("enabled", False):
            plugin_map["media_optimizer"] = (
                MediaOptimizationPlugin,
                {"config": mol_config},
            )
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
        timeout_seconds = None
        failure_policy = None
        depends_on: list[str] = getattr(plugin_cls, "depends_on", [])
        plugin_config: dict = {}

        if cfg:
            enabled = cfg.get("enabled", True)
            priority = cfg.get("priority", 0)
            timeout_seconds = cfg.get("timeout_seconds")
            failure_policy = cfg.get("failure_policy")
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
            timeout_seconds=timeout_seconds,
            failure_policy=failure_policy,
        )

    try:
        from aigateway_core.pipelines.generation.registration import (
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
            logger.info(
                "Generation Optimization Layer 已禁用 "
                "(generation_optimization.enabled=false)"
            )
    except ImportError as exc:
        logger.debug("Generation Optimization Plugins 不可用（导入失败）: %s", exc)
