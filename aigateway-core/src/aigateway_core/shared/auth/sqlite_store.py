"""Public SQLite auth store with deterministic config-backed path resolution."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from aigateway_core.shared.runtime_values import configured_path

from . import _sqlite_store_impl as _impl


class SQLiteStore(_impl.SQLiteStore):
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

    def _accumulate_quota(
        self,
        quota: dict | None,
        tokens: int,
        cost: float,
        model: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> dict:
        """Persist pricing provenance alongside numeric quota counters.

        Monthly cost remains numeric for compatibility. ``model_usage`` records
        explicit ``free_requests`` and ``unpriced_requests`` counters so missing
        pricing is not silently indistinguishable from a configured free model.
        """
        updated = super()._accumulate_quota(
            quota,
            tokens,
            cost,
            model,
            tokens_in,
            tokens_out,
        )
        pricing_status = getattr(cost, "pricing_status", None)
        if pricing_status not in {"priced", "free", "unpriced"}:
            return updated

        raw_model_usage = updated.get("model_usage", "{}")
        try:
            model_usage = (
                json.loads(raw_model_usage)
                if isinstance(raw_model_usage, str)
                else dict(raw_model_usage or {})
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            model_usage = {}

        entry: dict[str, Any]
        current = model_usage.get(model)
        if isinstance(current, dict):
            entry = current
        else:
            entry = {"in": tokens_in, "out": tokens_out}
        entry["pricing_status"] = pricing_status
        counter_name = f"{pricing_status}_requests"
        entry[counter_name] = int(entry.get(counter_name, 0)) + 1
        model_usage[model] = entry
        updated["model_usage"] = json.dumps(model_usage, ensure_ascii=False)
        return updated


# Preserve constants, helper functions and data models imported from this module
# before the implementation was split. Only the configured class is overridden.
for _name in dir(_impl):
    if _name.startswith("__") or _name == "SQLiteStore":
        continue
    if _name not in globals():
        globals()[_name] = getattr(_impl, _name)

__all__ = [
    name
    for name in globals()
    if not name.startswith("_") and name not in {"Path", "Any"}
]
