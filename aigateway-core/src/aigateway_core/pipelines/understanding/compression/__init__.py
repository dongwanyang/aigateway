"""Prompt token compression (LLMLingua-2) - part of the understanding pipeline.

Authoritative implementation: ``aigateway_core.pipelines.understanding.compression.plugin``.
"""
from functools import wraps

from aigateway_core.pipelines.understanding.compression.plugin import (
    PromptCompressPlugin,
)

_original_init_compressor = PromptCompressPlugin._init_compressor


@wraps(_original_init_compressor)
def _init_configured_compressor(self) -> None:
    if not str(getattr(self._config, "model_name", "")).strip():
        self._is_available = False
        return
    _original_init_compressor(self)


PromptCompressPlugin._init_compressor = _init_configured_compressor

__all__ = ["PromptCompressPlugin"]
