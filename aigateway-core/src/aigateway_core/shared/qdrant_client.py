"""Public Qdrant client with config-backed defaults."""
from __future__ import annotations

import logging
import os
from typing import Any

from aigateway_core.shared.runtime_values import (
    configured_number,
    configured_text,
    get_runtime_value,
)

from . import _qdrant_client_impl as _impl

# Preserve the established module-level test seam. Existing callers may replace
# ``qdrant_client.AsyncClient`` with a fake client without reaching into the
# private implementation module.
AsyncClient = _impl.AsyncClient
Timeout = _impl.Timeout
logger = logging.getLogger(__name__)


class QdrantClientManager(_impl.QdrantClientManager):
    """Qdrant manager that resolves omitted deployment values from config.yaml."""

    def __init__(self) -> None:
        super().__init__()
        self.url = ""

    def _configured_api_key(self) -> str | None:
        for env_name in ("QDRANT_API_KEY", "AI_GATEWAY_QDRANT_API_KEY"):
            value = os.environ.get(env_name, "").strip()
            if value:
                return value
        try:
            configured = get_runtime_value(
                "infrastructure.qdrant.api_key",
                required=False,
            )
        except RuntimeError:
            return None
        if isinstance(configured, str) and configured.strip():
            return configured.strip()
        return None

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        api_key = self._configured_api_key()
        if api_key:
            headers["api-key"] = api_key
        return headers

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
        ).rstrip("/")
        selected_connect_timeout = (
            float(connect_timeout)
            if connect_timeout is not None
            else float(configured_number("infrastructure.qdrant.connect_timeout"))
        )
        selected_read_timeout = (
            float(read_timeout)
            if read_timeout is not None
            else float(configured_number("infrastructure.qdrant.read_timeout"))
        )
        selected_write_timeout = (
            float(write_timeout)
            if write_timeout is not None
            else float(configured_number("infrastructure.qdrant.write_timeout"))
        )

        self.url = selected_url
        client_kwargs: dict[str, Any] = {
            "base_url": self.url,
            "timeout": Timeout(
                connect=selected_connect_timeout,
                read=selected_read_timeout,
                write=selected_write_timeout,
                pool=5.0,
            ),
        }
        # Only pass constructor headers when authentication requires defaults on
        # health/list requests. Keeping the unauthenticated constructor minimal
        # preserves lightweight AsyncClient-compatible adapters and test seams.
        api_key = self._configured_api_key()
        if api_key:
            client_kwargs["headers"] = {
                "Content-Type": "application/json",
                "api-key": api_key,
            }
        self._http = AsyncClient(**client_kwargs)
        try:
            response = await self._http.get("/")
            response.raise_for_status()
            logger.info("Qdrant 连接成功: %s", self.url)
        except Exception as exc:
            await self._http.aclose()
            self._http = None
            raise ConnectionError(f"Qdrant 连接失败 ({self.url}): {exc}") from exc

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


# Re-export non-private implementation symbols for source compatibility.
for _name in dir(_impl):
    if _name.startswith("_") or _name in {
        "AsyncClient",
        "Timeout",
        "QdrantClientManager",
        "logger",
    }:
        continue
    if _name not in globals():
        globals()[_name] = getattr(_impl, _name)

__all__ = ["AsyncClient", "QdrantClientManager", "Timeout"]
