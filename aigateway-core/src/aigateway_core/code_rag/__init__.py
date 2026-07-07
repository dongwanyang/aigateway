"""Code RAG 子系统 helper 模块.

包含:
- embedding_router: 嵌入模型 → collection slug/维度/编码
- splitter:         LangChain 加载器 + AST 语义切分 + 路径白名单 + 行号跨度
- graph_builder:    CodeGraph SQLite 图谱库构建
- graph_query:      从图谱库解析符号级 callers/callees/imports

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
from .graph_query import lookup_symbol_metadata
from .splitter import (
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
    "is_path_allowed",
    "compute_line_span",
    "build_code_graph",
    "lookup_symbol_metadata",
]
