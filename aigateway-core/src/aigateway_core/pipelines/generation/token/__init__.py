"""Token / feature compression + template + preview — part of generation pipeline."""
from aigateway_core.generation_optimization.strategies import (
    token_compressor as _s_token,
    feature_cache as _s_fcache,
    prompt_confirmation as _s_confirm,
    prompt_template_manager as _s_tmpl,
    video_preview as _s_video,
)
from aigateway_core.generation_optimization.plugins import (
    token_compressor_plugin as _p_token,
)

_sources = (_s_token, _s_fcache, _s_confirm, _s_tmpl, _s_video, _p_token)
_names: list[str] = []
for _src in _sources:
    for _name in dir(_src):
        if _name.startswith("_"):
            continue
        if _name not in globals():
            globals()[_name] = getattr(_src, _name)
            _names.append(_name)

__all__ = _names
del _s_token, _s_fcache, _s_confirm, _s_tmpl, _s_video, _p_token
del _sources, _names, _src, _name
