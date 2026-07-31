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

import asyncio
import gc
import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# L3 向量计算 — Qwen/Qwen3-Embedding-0.6B (1024 维)
# ------------------------------------------------------------------

# 模块级模型缓存（避免每次请求加载 ~600MB 模型）
_l3_model_cache: dict[str, Any] = {}
_l3_model_lock = threading.Lock()

# L3 向量计算设备：cpu | cuda | auto（默认 auto——有 CUDA 用 CUDA，否则 CPU）。
# late-bind：由 main.py 启动时按 config embedding.device 调用 set_l3_device() 注入，
# 避免在本 core 层反向依赖 shared.config（参照 LiteLLMBridge.set_auto_resolver 模式）。
_l3_device: str = "auto"
_l3_model_name: str = "Qwen/Qwen3-Embedding-0.6B"
_l3_idle_unload_seconds: float = 300.0
_l3_idle_generation: int = 0
_l3_idle_task: asyncio.Task[None] | None = None


def set_l3_device(device: str) -> None:
    """注入 L3 embedding 推理设备（main.py 启动时调用一次）。

    必须在首次 _compute_l3_vector 调用前设置；模型一旦加载，device 即固化。
    无效值回落 auto。
    """
    global _l3_device
    dev = (device or "auto").strip().lower()
    if dev not in ("cpu", "cuda", "auto"):
        logger.warning("set_l3_device(%r) 不识别，回落 auto", device)
        dev = "auto"
    _l3_device = dev
    logger.info("L3 embedding device 设为: %s", dev)


def set_l3_model(model_name: str) -> None:
    """Set the configured L3 model path before the first model load."""
    global _l3_model_name
    if _l3_model_cache:
        raise RuntimeError("L3 embedding model is already loaded")
    _l3_model_name = model_name.strip() or "Qwen/Qwen3-Embedding-0.6B"


def set_l3_idle_unload_seconds(seconds: float) -> None:
    """Configure automatic model release after an idle interval.

    ``0`` disables automatic release.  The setting only affects future inference
    calls and never interrupts an active model invocation because release uses the
    same module lock as loading and inference.
    """
    global _l3_idle_unload_seconds
    value = float(seconds)
    if value < 0:
        raise ValueError("embedding.idle_unload_seconds must be >= 0")
    _l3_idle_unload_seconds = value


def release_l3_model() -> bool:
    """Release the cached L3 model and return whether anything was resident."""
    global _l3_idle_generation, _l3_idle_task
    _l3_idle_generation += 1
    # Incrementing the generation invalidates a pending idle timer.  Do not
    # call Task.cancel() here: this function can run in a worker thread and may
    # also have been invoked by the timer task itself.
    _l3_idle_task = None

    with _l3_model_lock:
        model = _l3_model_cache.pop("model", None)
        tokenizer = _l3_model_cache.pop("tokenizer", None)
        _l3_model_cache.pop("device", None)
        resident = model is not None or tokenizer is not None
        if model is not None:
            try:
                model.to("cpu")
            except Exception:
                pass
        del model, tokenizer

    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass
    return resident


def _schedule_idle_release() -> None:
    global _l3_idle_generation, _l3_idle_task
    if _l3_idle_unload_seconds <= 0:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _l3_idle_generation += 1
    generation = _l3_idle_generation
    previous = _l3_idle_task
    if previous is not None and not previous.done():
        previous.cancel()

    async def release_after_idle() -> None:
        try:
            await asyncio.sleep(_l3_idle_unload_seconds)
        except asyncio.CancelledError:
            return
        if generation != _l3_idle_generation:
            return
        await asyncio.to_thread(release_l3_model)
        logger.info("L3 embedding model released after %.0fs idle", _l3_idle_unload_seconds)

    _l3_idle_task = loop.create_task(release_after_idle())


async def _compute_l3_vector(text: str, *, load_if_missing: bool = True) -> list | None:
    """使用 Qwen/Qwen3-Embedding-0.6B 计算 1024 维 embedding 向量。

    使用 transformers + torch 直接加载（无需 sentence_transformers）。
    模型在首次调用时加载并缓存到模块级变量。

    Args:
        text: 待嵌入的文本（通常是 normalized_messages）。
        load_if_missing: False 时，若模型尚未加载则直接跳过，避免请求路径首
            次加载大模型阻塞单 worker。

    Returns:
        1024 维归一化向量列表，失败返回 None。
    """
    if not load_if_missing and "tokenizer" not in _l3_model_cache:
        return None
    result = await asyncio.to_thread(
        _compute_l3_vector_sync,
        text,
        load_if_missing,
    )
    if result is not None:
        _schedule_idle_release()
    return result


def _compute_l3_vector_sync(
    text: str,
    load_if_missing: bool = True,
) -> list | None:
    """在线程中加载并执行同步 transformers 模型。"""
    try:
        import torch
        from transformers import AutoModel, AutoTokenizer

        model_name = _l3_model_name
        local_only = os.path.isabs(model_name)

        # 模型对象跨请求共享；串行化加载与推理，避免重复冷加载和并发访问。
        with _l3_model_lock:
            if "tokenizer" not in _l3_model_cache:
                if not load_if_missing:
                    return None
                logger.info("Loading L3 embedding model: %s", model_name)
                _l3_model_cache["tokenizer"] = AutoTokenizer.from_pretrained(
                    model_name,
                    trust_remote_code=True,
                    local_files_only=local_only,
                )
                device = _l3_device
                if device == "cuda" and not torch.cuda.is_available():
                    raise RuntimeError("semantic_cache_cuda_unavailable")
                if device == "auto":
                    device = "cuda" if torch.cuda.is_available() else "cpu"
                _l3_model_cache["device"] = device
                _l3_model_cache["model"] = AutoModel.from_pretrained(
                    model_name,
                    trust_remote_code=True,
                    local_files_only=local_only,
                ).to(device).eval()
                logger.info("L3 embedding model loaded on device=%s", device)

            tokenizer = _l3_model_cache["tokenizer"]
            model = _l3_model_cache["model"]
            device = _l3_model_cache["device"]
            inputs = tokenizer(
                text,
                return_tensors="pt",
                truncation=True,
                max_length=512,
                padding=True,
            ).to(device)

            with torch.no_grad():
                outputs = model(**inputs)

            attention_mask = inputs["attention_mask"]
            token_embeddings = outputs.last_hidden_state
            input_mask_expanded = (
                attention_mask.unsqueeze(-1)
                .expand(token_embeddings.size())
                .float()
            )
            embedding = torch.sum(
                token_embeddings * input_mask_expanded, 1
            ) / torch.clamp(input_mask_expanded.sum(1), min=1e-9)
            embedding = torch.nn.functional.normalize(embedding, p=2, dim=1)
            return embedding[0].cpu().tolist()

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
    pipeline_version: str = "1",
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
            pipeline_version=pipeline_version,
        )
        logger.debug("L3 backfill success: key=%s", cache_key[:16])
    except Exception as exc:
        logger.warning("L3 backfill failed: %s", exc)
