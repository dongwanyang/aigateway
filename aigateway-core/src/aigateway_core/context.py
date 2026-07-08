"""Compatibility shim: PipelineContext and namespace constants now live in
``aigateway_core.dispatch.context``. This module re-exports every public name
from the new home so existing ``from aigateway_core.context import ...`` calls
keep working.
"""
from aigateway_core.dispatch.context import *  # noqa: F401,F403
from aigateway_core.dispatch.context import PipelineContext  # noqa: F401

# ``import *`` skips names prefixed with underscore; the dispatch module defines
# no underscore-prefixed public names, so the starred import covers all NS_*
# constants and PipelineContext. Re-list PipelineContext explicitly so static
# analyzers and ``__all__`` stay accurate.
__all__ = [
    "PipelineContext",
    "NS_PROMPT_COMPRESS",
    "NS_PROMPT_CACHE",
    "NS_SEMANTIC_CACHE",
    "NS_PII_DETECTOR",
    "NS_MODEL_ROUTER",
    "NS_MEDIA_OPTIMIZATION",
    "NS_GENERATION_PIPELINE",
    "NS_RAG_RETRIEVER",
    "NS_CONV_COMPRESSOR",
]
