from __future__ import annotations

from types import SimpleNamespace

from aigateway_api.dispatcher import _is_text_completion


def test_auto_request_with_generation_options_keeps_text_output_guard() -> None:
    body = SimpleNamespace(
        model="auto",
        generation_options=SimpleNamespace(duration_seconds=8, fps=8),
    )

    assert _is_text_completion(body) is True


def test_explicit_media_models_bypass_text_output_guard() -> None:
    assert _is_text_completion(SimpleNamespace(model="image-model")) is False
    assert _is_text_completion(SimpleNamespace(model="video-model")) is False
