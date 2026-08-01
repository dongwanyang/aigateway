"""Safe lifecycle management for the local RAG embedding model."""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any


class EmbeddingModelRuntime:
    """Serialize model loading and prevent release during active inference."""

    def __init__(self) -> None:
        self.cache: dict[str, Any] = {}
        self._condition = threading.Condition(threading.RLock())
        self._active = 0
        self._loading = False

    @contextmanager
    def lease(self, loader: Callable[[], Any]) -> Iterator[Any]:
        should_load = False
        with self._condition:
            while self._loading:
                self._condition.wait()
            model = self.cache.get("model")
            if model is None:
                self._loading = True
                should_load = True
            else:
                self._active += 1

        if should_load:
            try:
                loaded = loader()
            except Exception:
                with self._condition:
                    self._loading = False
                    self._condition.notify_all()
                raise
            with self._condition:
                model = self.cache.setdefault("model", loaded)
                self._loading = False
                self._active += 1
                self._condition.notify_all()

        try:
            yield model
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def release_if_idle(self) -> dict[str, bool]:
        """Detach the cached model only when no load or inference is active."""
        with self._condition:
            if self._loading or self._active:
                return {"released": False, "busy": True}
            model = self.cache.pop("model", None)

        if model is not None:
            try:
                model.to("cpu")
            except Exception:
                pass
        return {"released": model is not None, "busy": False}

    @property
    def active_count(self) -> int:
        with self._condition:
            return self._active


embedding_model_runtime = EmbeddingModelRuntime()


__all__ = ["EmbeddingModelRuntime", "embedding_model_runtime"]
