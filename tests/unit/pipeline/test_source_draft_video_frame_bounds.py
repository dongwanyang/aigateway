from types import SimpleNamespace

import pytest

from aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError
from aigateway_core.pipelines.generation.draft.source_draft_video import (
    _normalize_frames,
)


def _config(*, min_frames: int = 1, max_frames: int = 481):
    return SimpleNamespace(
        video_supported_durations_seconds=(3, 5, 8),
        video_max_fps=60,
        video_min_frames=min_frames,
        video_max_frames=max_frames,
    )


@pytest.mark.parametrize(
    "config",
    [
        _config(max_frames=41),
        _config(min_frames=66),
    ],
)
def test_source_video_timing_never_silently_changes_requested_duration(config):
    with pytest.raises(DraftWorkflowError, match="video_duration_unsupported"):
        _normalize_frames(config, duration_seconds=8, fps=8)


def test_source_video_timing_keeps_wan_compatible_rounding():
    assert _normalize_frames(_config(), duration_seconds=5, fps=8) == (5.0, 8, 41)
