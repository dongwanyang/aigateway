"""RAG retrieval plugin - part of the understanding pipeline.

Authoritative implementation: ``aigateway_core.pipelines.understanding.rag.rag_retriever_plugin``.
"""
from . import rag_retriever_plugin as _wrapped

_names: list[str] = []
for _name in dir(_wrapped):
    if _name.startswith("_"):
        continue
    if _name not in globals():
        globals()[_name] = getattr(_wrapped, _name)
        _names.append(_name)

__all__ = _names
del _wrapped, _names, _name
