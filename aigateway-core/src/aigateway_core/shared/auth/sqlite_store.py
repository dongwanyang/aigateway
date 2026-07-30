"""Public SQLite auth store with deterministic config-backed path resolution."""
from __future__ import annotations

import os
from pathlib import Path

from aigateway_core.shared.runtime_values import configured_path

from ._sqlite_store_impl import SQLiteStore as _BaseSQLiteStore


class SQLiteStore(_BaseSQLiteStore):
    """SQLite store resolving omitted paths relative to the active config file."""

    def __init__(self, db_path: str | None = None):
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
        super().__init__(db_path=resolved)


__all__ = ["SQLiteStore"]
