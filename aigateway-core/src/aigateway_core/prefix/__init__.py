"""Shared pre-routing layer (总 1).

See ``docs/superpowers/specs/2026-07-07-runtime-structure-design.md``.
Everything under this package runs *before* dispatch splits the request
into an understanding or generation pipeline: PII, cache lookup/backfill,
media preprocessing.
"""
from aigateway_core.prefix import pii, cache, media  # noqa: F401

__all__ = ["pii", "cache", "media"]
