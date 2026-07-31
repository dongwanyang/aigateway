"""Public prompt-template manager with config-backed Redis prefixes."""
from __future__ import annotations

from aigateway_core.shared.runtime_values import redis_key_prefix

from . import _prompt_template_manager_impl as _impl


class PromptTemplateManager(_impl.PromptTemplateManager):
    """Prompt template manager using instance-scoped configured key prefixes."""

    def __init__(self, *args, **kwargs):
        self.KEY_PREFIX = redis_key_prefix("prompt_template")
        self.INDEX_PREFIX = redis_key_prefix("prompt_template_index")
        super().__init__(*args, **kwargs)


for _name in dir(_impl):
    if _name.startswith("_") or _name == "PromptTemplateManager":
        continue
    if _name not in globals():
        globals()[_name] = getattr(_impl, _name)

__all__ = tuple(name for name in globals() if not name.startswith("_"))
