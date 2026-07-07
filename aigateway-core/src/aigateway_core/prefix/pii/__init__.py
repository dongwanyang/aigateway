"""PII detection / sanitize / reject — part of the shared prefix layer.

Authoritative implementations live in ``aigateway_core.security`` and
``aigateway_core.pipeline.PIIDetectorPlugin``.
"""
from aigateway_core import security as _security
from aigateway_core.pipeline import PIIDetectorPlugin

_public = [name for name in dir(_security) if not name.startswith("_")]
for _name in _public:
    globals()[_name] = getattr(_security, _name)

__all__ = _public + ["PIIDetectorPlugin"]
del _security, _public, _name
