"""Extend the OpenAI-compatible request model with source_draft_id."""
from __future__ import annotations

from pydantic import Field


def install_unified_source_contract() -> None:
    """Add the documented source_draft_id field before request validation.

    ``ChatCompletionRequest`` is already referenced by the route module, so the
    existing class object is rebuilt in place rather than replaced.
    """
    from . import openai_compat

    current = openai_compat.GenerationOptions
    if "source_draft_id" in getattr(current, "model_fields", {}):
        return

    class GenerationOptionsWithSource(current):
        source_draft_id: str | None = Field(
            default=None,
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
        )

    GenerationOptionsWithSource.__name__ = "GenerationOptions"
    openai_compat.GenerationOptions = GenerationOptionsWithSource
    request_model = openai_compat.ChatCompletionRequest
    request_model.__annotations__["generation_options"] = (
        GenerationOptionsWithSource | None
    )
    request_model.model_rebuild(force=True)


__all__ = ["install_unified_source_contract"]
