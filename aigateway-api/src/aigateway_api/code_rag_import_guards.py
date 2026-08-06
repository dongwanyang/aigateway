"""Fail closed when a real Code RAG import produced no visible repository.

The Code RAG worker persists task state in SQLite, vectors in Qdrant and the
repository-list metadata in Redis. A real import must not become ``completed``
unless it produced at least one chunk and the repository metadata was persisted.
Synthetic progress-callback tests without source metadata retain their legacy
state-transition behavior.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

_INSTALL_MARKER = "_aigateway_code_rag_import_guards_installed"
_ZERO_CHUNK_ERROR = (
    "未生成任何可索引代码符号；请检查源码类型、ignore_patterns、"
    "CodeGraph 解析结果，以及源码是否包含受支持的函数、方法或类"
)
_REPOSITORY_STORE_ERROR = (
    "Redis not connected: unable to persist code repository metadata"
)


def install_code_rag_import_guards() -> None:
    """Install strict completion and repository-persistence guards once."""
    from . import code_rag_routes as routes

    if getattr(routes, _INSTALL_MARKER, False):
        return

    original_write_task_state: Callable[
        [Any, str, dict[str, Any]], Awaitable[None]
    ] = routes._write_task_state
    original_append_repository: Callable[
        [Any, dict[str, Any]], Awaitable[None]
    ] = routes._append_repository

    async def write_task_state_strict(
        app_state: Any,
        task_id: str,
        fields: dict[str, Any],
    ) -> None:
        next_fields = dict(fields)
        if next_fields.get("status") == "completed":
            current = await routes._read_task_state(app_state, task_id)
            total_raw = next_fields.get("total")
            if total_raw is None and current is not None:
                total_raw = current.get("total")
            try:
                total = int(total_raw or 0)
            except (TypeError, ValueError):
                total = 0

            # Production import tasks always persist source metadata before the
            # worker starts. Keeping the guard scoped to those records avoids
            # changing low-level progress callback tests that intentionally use
            # a metadata-free synthetic task.
            source_type = str((current or {}).get("source_type") or "").strip()
            source_label = str((current or {}).get("source_label") or "").strip()
            is_real_import = bool(source_type or source_label)
            if total <= 0 and is_real_import:
                next_fields.update(
                    status="failed",
                    done=0,
                    total=0,
                    error=_ZERO_CHUNK_ERROR,
                )
        await original_write_task_state(app_state, task_id, next_fields)

    async def append_repository_strict(
        redis_mgr: Any,
        repo_meta: dict[str, Any],
    ) -> None:
        if redis_mgr is None or getattr(redis_mgr, "redis", None) is None:
            raise RuntimeError(_REPOSITORY_STORE_ERROR)
        await original_append_repository(redis_mgr, repo_meta)

    routes._write_task_state = write_task_state_strict
    routes._append_repository = append_repository_strict
    setattr(routes, _INSTALL_MARKER, True)


__all__ = [
    "install_code_rag_import_guards",
]
