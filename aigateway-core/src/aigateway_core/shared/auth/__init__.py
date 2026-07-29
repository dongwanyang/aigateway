"""Authentication / authorization utilities — shared layer.

The auth SQLite location is a deployment value. Resolve it from an explicit
constructor argument, ``AI_GATEWAY_AUTH_DB_PATH``, or ``auth.database_path`` in
config.yaml; never infer it from the process working directory.
"""
from __future__ import annotations

import os
from functools import wraps
from pathlib import Path

from aigateway_core.shared.runtime_values import configured_path

from . import sqlite_store as _sqlite_store

_original_sqlite_store_init = _sqlite_store.SQLiteStore.__init__


@wraps(_original_sqlite_store_init)
def _configured_sqlite_store_init(self, db_path: str | None = None):
    selected = db_path or os.environ.get("AI_GATEWAY_AUTH_DB_PATH", "").strip()
    if selected:
        path = Path(selected).expanduser()
        if not path.is_absolute():
            config_file = Path(
                os.environ.get("AI_GATEWAY_CONFIG_PATH", "./config.yaml")
            ).expanduser().resolve()
            path = config_file.parent / path
        resolved = str(path.resolve())
    else:
        resolved = configured_path("auth.database_path")
    _original_sqlite_store_init(self, db_path=resolved)


_sqlite_store.SQLiteStore.__init__ = _configured_sqlite_store_init

__all__ = ["SQLiteStore"]
SQLiteStore = _sqlite_store.SQLiteStore
