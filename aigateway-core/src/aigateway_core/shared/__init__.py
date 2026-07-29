"""Cross-layer utilities.

Runtime-layer home for the 总分总 skeleton. Low-level service clients keep their
existing public APIs, while omitted deployment values are resolved lazily from
config.yaml rather than module constants.
"""
from __future__ import annotations

from functools import wraps

from . import qdrant_client as _qdrant

_original_qdrant_init = _qdrant.QdrantClientManager.__init__
_original_qdrant_connect = _qdrant.QdrantClientManager.connect


@wraps(_original_qdrant_init)
def _configured_qdrant_init(self) -> None:
    _original_qdrant_init(self)
    # An unconnected manager has no deployment URL. ``connect`` resolves it.
    self.url = ""


@wraps(_original_qdrant_connect)
async def _configured_qdrant_connect(
    self,
    url: str | None = None,
    connect_timeout: float | None = None,
    read_timeout: float | None = None,
    write_timeout: float | None = None,
) -> None:
    from aigateway_core.shared.runtime_values import (
        configured_number,
        configured_text,
    )

    selected_url = url.strip() if isinstance(url, str) and url.strip() else configured_text(
        "infrastructure.qdrant.url"
    )
    await _original_qdrant_connect(
        self,
        url=selected_url,
        connect_timeout=(
            float(connect_timeout)
            if connect_timeout is not None
            else float(configured_number("infrastructure.qdrant.connect_timeout"))
        ),
        read_timeout=(
            float(read_timeout)
            if read_timeout is not None
            else float(configured_number("infrastructure.qdrant.read_timeout"))
        ),
        write_timeout=(
            float(write_timeout)
            if write_timeout is not None
            else float(configured_number("infrastructure.qdrant.write_timeout"))
        ),
    )


async def _configured_upsert_collection(
    self,
    name: str,
    size: int | None = None,
    distance: str | None = None,
) -> bool:
    """Create a collection with vector/HNSW values from config.yaml."""
    from aigateway_core.shared.runtime_values import (
        configured_number,
        get_runtime_value,
    )

    if self._http is None:
        raise RuntimeError("Qdrant 尚未连接，请先调用 connect()")

    existing = await self._http.get("/collections/")
    existing.raise_for_status()
    collections = existing.json().get("result", {}).get("collections", [])
    if any(item.get("name") == name for item in collections):
        return True

    selected_distance = distance or get_runtime_value(
        "infrastructure.qdrant.distance"
    )
    normalized_distance = str(selected_distance).strip().lower().capitalize()
    if normalized_distance not in {"Cosine", "Dot", "Euclid", "Manhattan"}:
        raise RuntimeError("runtime_config_invalid:infrastructure.qdrant.distance")

    payload = {
        "vectors": {
            "size": (
                int(size)
                if size is not None
                else int(configured_number("embedding.vector_dim", int))
            ),
            "distance": normalized_distance,
        },
        "hnsw_config": {
            "m": int(configured_number("infrastructure.qdrant.hnsw_m", int)),
            "ef_construct": int(
                configured_number("infrastructure.qdrant.hnsw_ef_construct", int)
            ),
        },
    }
    response = await self._http.put(
        f"/collections/{name}",
        json=payload,
        headers=self._headers(),
    )
    response.raise_for_status()
    return True


_qdrant.QdrantClientManager.__init__ = _configured_qdrant_init
_qdrant.QdrantClientManager.connect = _configured_qdrant_connect
_qdrant.QdrantClientManager.upsert_collection = _configured_upsert_collection

QdrantClientManager = _qdrant.QdrantClientManager

__all__ = ["QdrantClientManager"]
