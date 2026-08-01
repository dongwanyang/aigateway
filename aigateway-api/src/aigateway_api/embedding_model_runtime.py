"""Safe lifecycle management for the local RAG embedding model."""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any


class EmbeddingModelRuntime:
    """Serialize model loading, inference leases, and device release."""

    def __init__(self) -> None:
        self.cache: dict[str, Any] = {}
        self._condition = threading.Condition(threading.RLock())
        self._active = 0
        self._loading = False
        self._releasing = False

    @contextmanager
    def lease(self, loader: Callable[[], Any]) -> Iterator[Any]:
        should_load = False
        with self._condition:
            while self._loading or self._releasing:
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
            except BaseException:
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

    def discard_invalid_if_idle(self, predicate: Callable[[Any], bool]) -> bool:
        """Discard an invalid cached object without racing active model work."""
        with self._condition:
            if self._loading or self._releasing or self._active:
                return False
            model = self.cache.get("model")
            if model is None or not predicate(model):
                return False
            self.cache.pop("model", None)
            return True

    def release_if_idle(self) -> dict[str, bool]:
        """Move the cached model to CPU only when no load or inference is active."""
        with self._condition:
            if self._loading or self._releasing or self._active:
                return {"released": False, "busy": True}
            model = self.cache.pop("model", None)
            if model is None:
                return {"released": False, "busy": False}
            # Keep new inference leases blocked until the old GPU model has
            # completed its device migration; otherwise two copies can overlap.
            self._releasing = True

        try:
            try:
                model.to("cpu")
            except Exception:
                pass
        finally:
            with self._condition:
                self._releasing = False
                self._condition.notify_all()
        return {"released": True, "busy": False}

    @property
    def active_count(self) -> int:
        with self._condition:
            return self._active


embedding_model_runtime = EmbeddingModelRuntime()


__all__ = ["EmbeddingModelRuntime", "embedding_model_runtime"]
