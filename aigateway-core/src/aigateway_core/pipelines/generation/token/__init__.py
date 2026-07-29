"""Token / feature compression + template + preview - part of generation pipeline.

Re-exports the strategy modules and the token-compressor plugin that live in
this package.
"""
from functools import wraps

from aigateway_core.shared.runtime_values import redis_key_prefix

from . import (
    feature_cache as _s_fcache,
)
from . import (
    prompt_confirmation as _s_confirm,
)
from . import (
    prompt_template_manager as _s_tmpl,
)
from . import (
    token_compressor as _s_token,
)
from . import token_compressor_plugin as _p_token
from . import (
    video_preview as _s_video,
)

# PromptTemplateManager retains its existing implementation and public API. Resolve
# instance prefixes from config only when a manager is created, avoiding import-time
# dependence on config.yaml.
_original_template_init = _s_tmpl.PromptTemplateManager.__init__


@wraps(_original_template_init)
def _configured_template_init(self, *args, **kwargs):
    self.KEY_PREFIX = redis_key_prefix("prompt_template")
    self.INDEX_PREFIX = redis_key_prefix("prompt_template_index")
    _original_template_init(self, *args, **kwargs)


_s_tmpl.PromptTemplateManager.__init__ = _configured_template_init

_sources = (_s_token, _s_fcache, _s_confirm, _s_tmpl, _s_video, _p_token)
_names: list[str] = []
for _src in _sources:
    for _name in dir(_src):
        if _name.startswith("_"):
            continue
        if _name not in globals():
            globals()[_name] = getattr(_src, _name)
            _names.append(_name)

__all__ = tuple(_names)
del _s_token, _s_fcache, _s_confirm, _s_tmpl, _s_video, _p_token
del _sources, _names, _src, _name
