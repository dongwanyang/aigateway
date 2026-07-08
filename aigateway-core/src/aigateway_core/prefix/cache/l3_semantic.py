"""L3 semantic-cache vector computation + async backfill.

Moved from ``aigateway_api.openai_compat`` (Task 5 runtime-structure
refactor) to fix a layering violation: core ``dispatch.dispatcher`` was
lazily importing ``_compute_l3_vector`` / ``_safe_l3_backfill`` from the
API surface. These helpers are L3 cache backfill logic and belong in the
core prefix/cache layer.

Relationship to ``cache_manager.CacheManager._safe_l3_backfill``:
  * ``CacheManager._safe_l3_backfill`` is a *method* on the cache manager
    that accepts a generic ``compute_embedding_fn`` callable. It is used
    by ``CacheManager.backfill_all_miss`` for the all-miss path.
  * The standalone ``_safe_l3_backfill`` here is the *dispatcher's* L3
    backfill: it hardcodes the Qwen3 embedding model via
    ``_compute_l3_vector`` and calls ``cache_manager.l3_store`` directly.
    The dispatcher calls this version (not the method) because the
    dispatcher already holds the cache_manager and computes the Qwen3
    vector inline.

The two do not conflict — different signatures, different call sites.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# L3 向量计算 — Qwen/Qwen3-Embedding-0.6B (1024 维)
# ------------------------------------------------------------------

# 模块级模型缓存（避免每次请求加载 ~600MB 模型）
_l3_model_cache: Dict[str, Any] = {}


async def _compute_l3_vector(text: str) -> Optional[list]:
    """使用 Qwen/Qwen3-Embedding-0.6B 计算 1024 维 embedding 向量。

    使用 transformers + torch 直接加载（无需 sentence_transformers）。
    模型在首次调用时加载并缓存到模块级变量。

    Args:
        text: 待嵌入的文本（通常是 normalized_messages）。

    Returns:
        1024 维归一化向量列表，失败返回 None。
    """
    try:
        import torch
        from transformers import AutoModel, AutoTokenizer

        model_name = "Qwen/Qwen3-Embedding-0.6B"

        # 从模块级缓存获取或加载模型
        if "tokenizer" not in _l3_model_cache:
            logger.info("Loading L3 embedding model: %s", model_name)
            _l3_model_cache["tokenizer"] = AutoTokenizer.from_pretrained(
                model_name, trust_remote_code=True
            )
            _l3_model_cache["model"] = AutoModel.from_pretrained(
                model_name, trust_remote_code=True
            ).eval()
            logger.info("L3 embedding model loaded successfully")

        tokenizer = _l3_model_cache["tokenizer"]
        model = _l3_model_cache["model"]

        # Tokenize（截断过长文本）
        inputs = tokenizer(
            text, return_tensors="pt", truncation=True, max_length=512, padding=True
        )

        # 推理
        with torch.no_grad():
            outputs = model(**inputs)

        # 使用 last_hidden_state 的 mean pooling 作为 sentence embedding
        attention_mask = inputs["attention_mask"]
        token_embeddings = outputs.last_hidden_state  # (1, seq_len, 1024)
        input_mask_expanded = attention_mask.unsqueeze(-1).expand(token_embeddings.size()).float()
        embedding = torch.sum(token_embeddings * input_mask_expanded, 1) / torch.clamp(
            input_mask_expanded.sum(1), min=1e-9
        )

        # L2 归一化
        embedding = torch.nn.functional.normalize(embedding, p=2, dim=1)

        return embedding[0].tolist()

    except ImportError as exc:
        logger.warning("L3 vector: transformers/torch not available: %s", exc)
        return None
    except Exception as exc:
        logger.warning("L3 vector computation failed: %s", exc)
        return None


async def _safe_l3_backfill(
    cache_manager: Any,
    cache_key: str,
    value_str: str,
    normalized_messages: str,
    model: str,
    user_id: str,
    token_count: int,
) -> None:
    """异步回填 L3 语义缓存（fire-and-forget）。

    使用 Qwen/Qwen3-Embedding-0.6B (transformers) 计算 1024 维向量后存入 Qdrant。
    失败时仅记录 WARNING，不影响主请求。
    """
    try:
        if cache_manager._qdrant_client is None:
            return

        # 计算 embedding 向量
        vector = await _compute_l3_vector(normalized_messages)
        if vector is None:
            return

        # 存入 Qdrant
        await cache_manager.l3_store(
            prompt_hash=cache_key,
            prompt_normalized=normalized_messages[:500],  # 截断避免 payload 过大
            model=model,
            response_json=value_str,
            user_id=user_id,
            token_count=token_count,
            vector=vector,
        )
        logger.debug("L3 backfill success: key=%s", cache_key[:16])
    except Exception as exc:
        logger.warning("L3 backfill failed: %s", exc)
