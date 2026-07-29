"""Cache-key v2 helpers.

Cache-key parameter buckets are runtime policy. Token edges are loaded from
``cache.key_buckets.max_tokens`` so cache semantics can be versioned and tuned
without changing source code.
"""
from __future__ import annotations

import re
import unicodedata

from aigateway_core.shared.runtime_values import get_runtime_value

# Temperature labels are semantic categories, not deployment capacities. They
# remain code-level algorithm definitions; max-token edges are configurable.
_TEMPERATURE_BUCKETS: list[tuple] = [
    (0.05, "exact_zero"),
    (0.3, "det"),
    (0.9, "bal"),
    (float("inf"), "cre"),
]
_MAX_TOKENS_BUCKETS: list[int] = []

_MODEL_SNAPSHOT_RE = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2}|latest)$")


def _configured_max_token_buckets() -> list[int]:
    raw = get_runtime_value("cache.key_buckets.max_tokens")
    if not isinstance(raw, list) or not raw:
        raise RuntimeError("runtime_config_invalid:cache.key_buckets.max_tokens")
    try:
        values = [int(item) for item in raw]
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "runtime_config_invalid:cache.key_buckets.max_tokens"
        ) from exc
    if any(item <= 0 for item in values) or values != sorted(set(values)):
        raise RuntimeError("runtime_config_invalid:cache.key_buckets.max_tokens")
    _MAX_TOKENS_BUCKETS[:] = values
    return _MAX_TOKENS_BUCKETS


def _bucket_temperature(t: float | None) -> str:
    """Map temperature to a coarse bucket. None treated as 1.0."""
    if t is None:
        t = 1.0
    for upper, name in _TEMPERATURE_BUCKETS:
        if t <= upper:
            return name
    return "cre"


def _bucket_max_tokens(mt: int | None) -> str:
    """Map max_tokens to the configured nearest bucket. None / 0 → any."""
    if not mt or mt <= 0:
        return "any"
    buckets = _configured_max_token_buckets()
    for edge in buckets:
        if mt <= edge:
            return f"le_{edge}"
    return f"gt_{buckets[-1]}"


def _model_family(model: str) -> str:
    """Extract family from model_id, stripping trailing date snapshots."""
    if not model:
        return ""
    if "/" in model:
        prefix, tail = model.rsplit("/", 1)
        return f"{prefix}/{_MODEL_SNAPSHOT_RE.sub('', tail)}"
    return _MODEL_SNAPSHOT_RE.sub("", model)


def _normalize_prompt(text: str) -> str:
    """Normalize prompt text with NFKC, whitespace collapse and strip."""
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", normalized).strip()


__all__ = [
    "_MAX_TOKENS_BUCKETS",
    "_MODEL_SNAPSHOT_RE",
    "_TEMPERATURE_BUCKETS",
    "_bucket_max_tokens",
    "_bucket_temperature",
    "_model_family",
    "_normalize_prompt",
]
