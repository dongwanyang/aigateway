"""Media preprocessing (OCR / transcription / document parsing).

Part of the shared prefix layer. Authoritative implementation lives in
``aigateway_core.media`` and its subpackages.
"""
from aigateway_core import media as _media
from aigateway_core.media import plugin  # re-expose the plugin submodule

_public = [name for name in dir(_media) if not name.startswith("_")]
for _name in _public:
    globals()[_name] = getattr(_media, _name)

__all__ = _public + ["plugin"]
del _media, _public, _name
