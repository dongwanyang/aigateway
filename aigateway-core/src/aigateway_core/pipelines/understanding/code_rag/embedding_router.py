"""嵌入模型 → 集合名 / 维度 / 编码封装.

每个嵌入模型独立 Qdrant 集合(rag_code_<slug>),避免 vector_dim 冲突。
sentence-transformers 走 lru_cache 缓存,避免每次导入都重新加载 ~600MB 权重。
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Any


def materialize_model_slug(model_name: str) -> str:
    """把嵌入模型名规范化为 Qdrant collection slug.

    非字母数字全部折叠成 '_',前后 '_' 剥掉,大小写统一小写。
    "Qwen/Qwen3-Embedding-0.6B" → "qwen_qwen3_embedding_0_6b"
    """
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", model_name.strip().lower())
    return normalized.strip("_")


def resolve_collection_name(model_name: str) -> str:
    """返回该嵌入模型对应的 Qdrant 集合名."""
    return f"rag_code_{materialize_model_slug(model_name)}"


@lru_cache(maxsize=8)
def _get_model(model_name: str) -> Any:
    """加载 sentence-transformers 模型(lazy import,避免开发机强依赖)."""
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(model_name)


def probe_embedding_dimension(model_name: str) -> int:
    """编码一次探测向量,取维度。用于建 Qdrant 集合前确定 vector_size。"""
    vector = _get_model(model_name).encode(
        ["dimension probe"],
        normalize_embeddings=True,
        show_progress_bar=False,
    )[0]
    return len(vector)


def encode_texts(model_name: str, texts: list[str]) -> list[list[float]]:
    """批量编码文本,返回归一化向量列表(list[list[float]])."""
    vectors = _get_model(model_name).encode(
        texts,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    return vectors.tolist()
