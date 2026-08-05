from __future__ import annotations

import pytest

from aigateway_api import verified_draft_cancellation as cancellation


def test_queue_payload_requires_both_queue_lists() -> None:
    with pytest.raises(ValueError, match="invalid_comfyui_queue_payload"):
        cancellation._active_queue_prompt_ids({})

    with pytest.raises(ValueError, match="invalid_comfyui_queue_payload"):
        cancellation._active_queue_prompt_ids({"queue_running": []})

    with pytest.raises(ValueError, match="invalid_comfyui_queue_payload"):
        cancellation._active_queue_prompt_ids({"queue_pending": []})


def test_queue_payload_rejects_non_list_entries() -> None:
    with pytest.raises(ValueError, match="invalid_comfyui_queue_payload"):
        cancellation._active_queue_prompt_ids(
            {"queue_running": {}, "queue_pending": []}
        )

    with pytest.raises(ValueError, match="invalid_comfyui_queue_payload"):
        cancellation._active_queue_prompt_ids(
            {"queue_running": [], "queue_pending": None}
        )


def test_queue_payload_extracts_running_and_pending_prompt_ids() -> None:
    assert cancellation._active_queue_prompt_ids(
        {
            "queue_running": [[0, "running-1", {}, {}, []]],
            "queue_pending": [[1, "pending-1", {}, {}, []]],
        }
    ) == {"running-1", "pending-1"}
