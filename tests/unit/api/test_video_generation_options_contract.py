from __future__ import annotations

import pytest
from aigateway_api.openai_compat import ChatCompletionRequest
from pydantic import ValidationError


def _request_payload(duration_seconds: object, fps: object = 8) -> dict[str, object]:
    return {
        "model": "auto",
        "messages": [{"role": "user", "content": "生成一段视频"}],
        "generation_options": {
            "duration_seconds": duration_seconds,
            "fps": fps,
        },
    }


@pytest.mark.parametrize("duration_seconds", [3, 5, 8])
def test_video_timing_survives_api_request_validation(
    duration_seconds: int,
) -> None:
    body = ChatCompletionRequest.model_validate(
        _request_payload(duration_seconds),
    )

    assert body.generation_options is not None
    assert body.generation_options.duration_seconds == duration_seconds
    assert body.generation_options.fps == 8
    assert body.generation_options.model_dump(exclude_none=True) == {
        "backend": "auto",
        "prompt_mode": "auto",
        "quality": "standard",
        "duration_seconds": duration_seconds,
        "fps": 8,
    }


@pytest.mark.parametrize("duration_seconds", [0, 4, 9, True, "5"])
def test_api_rejects_unsupported_video_durations(duration_seconds: object) -> None:
    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(
            _request_payload(duration_seconds),
        )


@pytest.mark.parametrize("fps", [0, 61, True, 8.0, "8"])
def test_api_rejects_invalid_video_fps(fps: object) -> None:
    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(
            _request_payload(5, fps=fps),
        )
