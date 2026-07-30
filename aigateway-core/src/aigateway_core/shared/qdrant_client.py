"""Public Qdrant client with config-backed defaults."""
from __future__ import annotations

from aigateway_core.shared.runtime_values import (
    configured_number,
    configured_text,
    get_runtime_value,
)

from ._qdrant_client_impl import QdrantClientManager as _BaseQdrantClientManager


class QdrantClientManager(_BaseQdrantClientManager):
    """Qdrant manager that resolves omitted deployment values from config.yaml."""

    def __init__(self) -> None:
        super().__init__()
        self.url = ""

    async def connect(
        self,
        url: str | None = None,
        connect_timeout: float | None = None,
        read_timeout: float | None = None,
        write_timeout: float | None = None,
    ) -> None:
        selected_url = (
            url.strip()
            if isinstance(url, str) and url.strip()
            else configured_text("infrastructure.qdrant.url")
        )
        await super().connect(
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

    async def upsert_collection(
        self,
        name: str,
        size: int | None = None,
        distance: str | None = None,
    ) -> bool:
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
            raise RuntimeError(
                "runtime_config_invalid:infrastructure.qdrant.distance"
            )

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
                "m": int(
                    configured_number("infrastructure.qdrant.hnsw_m", int)
                ),
                "ef_construct": int(
                    configured_number(
                        "infrastructure.qdrant.hnsw_ef_construct", int
                    )
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


__all__ = ["QdrantClientManager"]
