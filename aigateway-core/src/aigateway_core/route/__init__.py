"""Unified routing + response closure (总 2).

See ``docs/superpowers/specs/2026-07-07-runtime-structure-design.md``.
This layer covers model resolution, LiteLLM bridge, provider fallback,
streaming/response assembly, and quota/metrics closure. This refactor
stakes the core-side location for model resolution and bridge only;
migrating streaming/response/quota out of the API surface is a later
phase.
"""
from aigateway_core.route import model_resolution, bridge  # noqa: F401

__all__ = ["model_resolution", "bridge"]
