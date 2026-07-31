"""Public token compressor with explicit empty CLIP-model handling."""
from __future__ import annotations

from . import _token_compressor_impl as _impl


class TokenCompressorStrategy(_impl.TokenCompressorStrategy):
    """Token compressor that uses hash fallback when CLIP is unconfigured."""

    def _load_clip_model(self) -> None:
        if not str(getattr(self._clip_config, "model_name", "")).strip():
            self._clip_model = None
            self._clip_processor = None
            self._clip_available = False
            return
        super()._load_clip_model()


for _name in dir(_impl):
    if _name.startswith("_") or _name == "TokenCompressorStrategy":
        continue
    if _name not in globals():
        globals()[_name] = getattr(_impl, _name)

__all__ = tuple(name for name in globals() if not name.startswith("_"))
