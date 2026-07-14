"""Code RAG 子系统 helper 模块.

包含:
- embedding_router: 嵌入模型 → collection slug/维度/编码
- splitter:         codegraph 行号切源码 + 结构描述构造(重构后)
- graph_builder:    codegraph CLI 构建图谱(保留整个 .codegraph/ 目录)
- graph_query:      通过 codegraph CLI 子进程查 callers/callees/imports/impact

wrapper 模块统一走 lazy import(codegraph / gitpython / sentence-transformers
在生产镜像里才装),这样纯逻辑单测在开发机上就能跑。
"""

from .embedding_router import (
    encode_texts,
    materialize_model_slug,
    probe_embedding_dimension,
    resolve_collection_name,
)
from .graph_builder import build_code_graph
from .graph_query import (
    get_callers,
    get_callees,
    get_impact,
    get_node,
    list_files,
    lookup_symbol_metadata,
    lookup_symbol_metadata_strict,
    lookup_related_symbols,
    lookup_related_symbols_strict,
    query_symbols,
    read_file_hashes,
    run_codegraph_sync,
)
from .splitter import (
    build_symbol_chunks,
    compute_line_span,
    is_path_allowed,
    split_code_directory,
)

__all__ = [
    "materialize_model_slug",
    "resolve_collection_name",
    "probe_embedding_dimension",
    "encode_texts",
    "split_code_directory",
    "build_symbol_chunks",
    "is_path_allowed",
    "compute_line_span",
    "build_code_graph",
    "lookup_symbol_metadata",
    "lookup_symbol_metadata_strict",
    "lookup_related_symbols",
    "lookup_related_symbols_strict",
    "query_symbols",
    "get_callers",
    "get_callees",
    "get_impact",
    "get_node",
    "list_files",
    "read_file_hashes",
    "run_codegraph_sync",
]

