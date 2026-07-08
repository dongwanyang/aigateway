"""Cache-key v2 helpers.

Moved from ``aigateway_core.caching`` as part of the 总分总 runtime split
(Task 3). These are lightweight, dependency-free functions used by
``CacheManager.generate_cache_key`` and the dispatcher to build normalized
cache keys.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import List, Optional

# ------------------------------------------------------------------
# Cache key v2 constants
# ------------------------------------------------------------------
# Parameter bucketing: coarse-grained merge so minor SDK default
# differences fall into the same bucket.
_TEMPERATURE_BUCKETS: List[tuple] = [
    (0.05, "exact_zero"),   # <= 0.05 treated as deterministic, own bucket
    (0.3,  "det"),          # 0.05 ~ 0.3 low determinism
    (0.9,  "bal"),          # 0.3 ~ 0.9 balanced
    (float("inf"), "cre"),  # > 0.9 creative
]
_MAX_TOKENS_BUCKETS: List[int] = [256, 512, 1024, 2048, 4096, 8192, 16384]

# model_family: strip trailing date snapshot, e.g. gpt-4o-2024-08-06 → gpt-4o,
# claude-3-5-sonnet-20241022 → claude-3-5-sonnet.
# Matches:
#   -YYYYMMDD           e.g. 20241022
#   -YYYY-MM-DD         e.g. 2024-08-06
#   -latest             e.g. gpt-4-latest (some vendors)
_MODEL_SNAPSHOT_RE = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2}|latest)$")


def _bucket_temperature(t: Optional[float]) -> str:
    """Map temperature to a coarse bucket. None treated as 1.0 (OpenAI default)."""
    if t is None:
        t = 1.0
    for upper, name in _TEMPERATURE_BUCKETS:
        if t <= upper:
            return name
    return "cre"


def _bucket_max_tokens(mt: Optional[int]) -> str:
    """Map max_tokens to nearest bucket. None / 0 → any."""
    if not mt or mt <= 0:
        return "any"
    # Round up; beyond max edge → max edge
    for edge in _MAX_TOKENS_BUCKETS:
        if mt <= edge:
            return f"le_{edge}"
    return f"gt_{_MAX_TOKENS_BUCKETS[-1]}"


def _model_family(model: str) -> str:
    """Extract family from model_id, stripping trailing date snapshot.

    - gpt-4o                       → gpt-4o
    - gpt-4o-2024-08-06            → gpt-4o
    - gpt-4o-mini-2024-07-18       → gpt-4o-mini
    - claude-3-5-sonnet-20241022   → claude-3-5-sonnet
    - claude-sonnet-4-5-20250929   → claude-sonnet-4-5
    - auto                         → auto (special value, unchanged)
    - openai/gpt-4o                → openai/gpt-4o (provider prefix preserved)
    """
    if not model:
        return ""
    # Preserve provider/ prefix, only process the model part
    if "/" in model:
        prefix, tail = model.rsplit("/", 1)
        return f"{prefix}/{_MODEL_SNAPSHOT_RE.sub('', tail)}"
    return _MODEL_SNAPSHOT_RE.sub("", model)


def _normalize_prompt(text: str) -> str:
    """Normalize prompt text: NFKC + collapse whitespace + strip.

    - NFKC: unify full/half-width, combining characters (e.g. "ａ" → "a")
    - Multiple consecutive whitespace (incl. tabs/newlines) collapsed to one space
    - Leading/trailing whitespace removed

    Purpose: let semantically-equivalent prompts with minor formatting
    differences produce the same hash.
    """
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", text)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


__all__ = [
    "_MODEL_SNAPSHOT_RE",
    "_TEMPERATURE_BUCKETS",
    "_MAX_TOKENS_BUCKETS",
    "_bucket_temperature",
    "_bucket_max_tokens",
    "_model_family",
    "_normalize_prompt",
]
