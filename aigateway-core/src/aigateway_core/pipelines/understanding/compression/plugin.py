"""Public prompt compression plugin with explicit empty-model handling."""
from __future__ import annotations

from ._plugin_impl import PromptCompressPlugin as _BasePromptCompressPlugin


class PromptCompressPlugin(_BasePromptCompressPlugin):
    """Prompt compressor that disables itself when no model is configured."""

    def _init_compressor(self) -> None:
        if not str(getattr(self._config, "model_name", "")).strip():
            self._is_available = False
            return
        super()._init_compressor()


__all__ = ["PromptCompressPlugin"]
