#!/usr/bin/env python3
"""Small OpenAI-compatible MPS embedding service for Apple Silicon installs."""

from __future__ import annotations

import asyncio
import hmac
import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated

import torch
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from sentence_transformers import CrossEncoder, SentenceTransformer

MODEL_NAME = os.getenv(
    "AIGATEWAY_EMBEDDING_MODEL",
    "Qwen/Qwen3-Embedding-0.6B",
)
API_KEY = os.getenv("AIGATEWAY_EMBEDDING_API_KEY", "local-mps")
MAX_BATCH = int(os.getenv("AIGATEWAY_EMBEDDING_MAX_BATCH", "64"))
RERANK_MODEL = os.getenv("AIGATEWAY_RERANK_MODEL", "")
_semaphore = asyncio.Semaphore(1)

app = FastAPI(title="AI Gateway Native Embedding", docs_url=None, redoc_url=None)


class EmbeddingRequest(BaseModel):
    input: str | list[str]
    model: str = MODEL_NAME


class EmbeddingItem(BaseModel):
    object: str = "embedding"
    index: int
    embedding: list[float]


class EmbeddingResponse(BaseModel):
    object: str = "list"
    data: list[EmbeddingItem]
    model: str
    usage: dict[str, int] = Field(
        default_factory=lambda: {"prompt_tokens": 0, "total_tokens": 0}
    )


class RerankRequest(BaseModel):
    query: str
    documents: list[str]
    model: str = ""
    top_n: int | None = None


class RerankResult(BaseModel):
    index: int
    relevance_score: float
    document: str


class RerankResponse(BaseModel):
    model: str
    results: list[RerankResult]


def _authorize(authorization: str | None) -> None:
    supplied = ""
    if authorization and authorization.startswith("Bearer "):
        supplied = authorization[7:]
    if not hmac.compare_digest(supplied, API_KEY):
        raise HTTPException(status_code=401, detail="invalid embedding credential")


@lru_cache(maxsize=1)
def _model() -> SentenceTransformer:
    if not torch.backends.mps.is_available():
        raise RuntimeError("embedding_mps_unavailable")
    model_path = Path(MODEL_NAME)
    if not model_path.is_dir():
        raise RuntimeError("embedding_model_not_installed")
    return SentenceTransformer(
        str(model_path),
        device="mps",
        local_files_only=True,
    )


@lru_cache(maxsize=1)
def _reranker() -> CrossEncoder:
    if not torch.backends.mps.is_available():
        raise RuntimeError("reranker_mps_unavailable")
    model_path = Path(RERANK_MODEL)
    if not RERANK_MODEL or not model_path.is_dir():
        raise RuntimeError("reranker_model_not_installed")
    return CrossEncoder(
        str(model_path),
        device="mps",
        local_files_only=True,
    )


@app.on_event("startup")
async def startup() -> None:
    await asyncio.to_thread(_model)


@app.get("/health")
async def health() -> dict[str, object]:
    return {
        "status": "healthy",
        "device": "mps",
        "model": MODEL_NAME,
        "mps_available": torch.backends.mps.is_available(),
        "reranker_configured": bool(RERANK_MODEL),
    }


@app.post("/v1/embeddings", response_model=EmbeddingResponse)
async def embeddings(
    body: EmbeddingRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> EmbeddingResponse:
    _authorize(authorization)
    texts = [body.input] if isinstance(body.input, str) else body.input
    if not texts or len(texts) > MAX_BATCH:
        raise HTTPException(
            status_code=400,
            detail=f"input batch must contain 1..{MAX_BATCH} items",
        )
    async with _semaphore:
        try:
            vectors = await asyncio.to_thread(
                _model().encode,
                texts,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
        except RuntimeError as exc:
            if "out of memory" in str(exc).lower():
                torch.mps.empty_cache()
                raise HTTPException(
                    status_code=503,
                    detail="embedding_mps_out_of_memory",
                    headers={"Retry-After": "5"},
                ) from exc
            raise
    return EmbeddingResponse(
        data=[
            EmbeddingItem(index=index, embedding=vector.tolist())
            for index, vector in enumerate(vectors)
        ],
        model=body.model,
    )


@app.post("/v1/rerank", response_model=RerankResponse)
async def rerank(
    body: RerankRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> RerankResponse:
    _authorize(authorization)
    if not body.query or not body.documents or len(body.documents) > MAX_BATCH:
        raise HTTPException(
            status_code=400,
            detail=f"documents must contain 1..{MAX_BATCH} items",
        )
    async with _semaphore:
        try:
            pairs = [(body.query, document) for document in body.documents]
            scores = await asyncio.to_thread(_reranker().predict, pairs)
        except RuntimeError as exc:
            if "out of memory" in str(exc).lower():
                torch.mps.empty_cache()
                raise HTTPException(
                    status_code=503,
                    detail="reranker_mps_out_of_memory",
                    headers={"Retry-After": "5"},
                ) from exc
            detail = (
                str(exc)
                if str(exc) in {
                    "reranker_mps_unavailable",
                    "reranker_model_not_installed",
                }
                else "reranker_mps_failed"
            )
            raise HTTPException(status_code=503, detail=detail) from exc
    ranked = sorted(
        enumerate(scores),
        key=lambda item: float(item[1]),
        reverse=True,
    )
    limit = min(body.top_n or len(ranked), len(ranked))
    return RerankResponse(
        model=body.model or RERANK_MODEL,
        results=[
            RerankResult(
                index=index,
                relevance_score=float(score),
                document=body.documents[index],
            )
            for index, score in ranked[:limit]
        ],
    )
