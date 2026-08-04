from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from aigateway_api.draft_confirm_routes import confirm_draft
from aigateway_api.video_generation_observability import video_submission_fields
from aigateway_api.video_request_guard import reference_image_required
from aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError
from aigateway_core.pipelines.generation._common.models import VideoSubmitResult


def test_missing_anaphoric_video_reference_is_rejected():
    body = SimpleNamespace(
        messages=[{"role": "user", "content": "根据这张图生成一个 5 秒视频"}],
        generation_options={},
    )

    assert reference_image_required(body) is True


@pytest.mark.parametrize(
    "content",
    [
        "请解释这张图片和视频的区别",
        "这张图片适合做视频封面吗",
        "What is the difference between this image and a video?",
    ],
)
def test_non_generation_image_video_questions_are_not_rejected(content):
    body = SimpleNamespace(
        messages=[{"role": "user", "content": content}],
        generation_options={},
    )

    assert reference_image_required(body) is False


def test_uploaded_image_or_source_draft_satisfies_reference_requirement():
    uploaded = SimpleNamespace(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "让这张图动起来"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,aW1hZ2U="},
                    },
                ],
            }
        ],
        generation_options={},
    )
    sourced = SimpleNamespace(
        messages=[{"role": "user", "content": "根据这张图生成视频"}],
        generation_options={"source_draft_id": "image-draft"},
    )

    assert reference_image_required(uploaded) is False
    assert reference_image_required(sourced) is False


@pytest.mark.asyncio
async def test_confirm_route_preserves_keyframe_integrity_error_code():
    draft = SimpleNamespace(
        draft_id="video-draft",
        user_id="user-1",
        group_id="group-1",
        media_type="video",
        generation_params={"trace_id": "trace-1"},
    )
    strategy = SimpleNamespace(
        get_draft=AsyncMock(return_value=draft),
        confirm_draft=AsyncMock(
            side_effect=DraftWorkflowError("video_keyframe_integrity_mismatch")
        ),
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(draft_generator_strategy=strategy)
        )
    )

    with pytest.raises(HTTPException) as raised:
        await confirm_draft(
            "video-draft",
            request,
            auth={"user_id": "user-1", "group_id": "group-1"},
        )

    assert raised.value.status_code == 409
    assert raised.value.detail["error"]["code"] == (
        "video_keyframe_integrity_mismatch"
    )
    strategy.confirm_draft.assert_awaited_once_with("video-draft")


@pytest.mark.asyncio
async def test_confirm_route_keeps_video_audit_log_contract():
    from aigateway_api import openai_compat

    draft = SimpleNamespace(
        draft_id="video-draft",
        user_id="user-1",
        group_id="group-1",
        media_type="video",
        generation_params={},
    )
    strategy = SimpleNamespace(
        get_draft=AsyncMock(return_value=draft),
        confirm_draft=AsyncMock(
            return_value=VideoSubmitResult(
                draft_id="video-draft",
                video_id="video-123",
                status="generating",
            )
        ),
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(draft_generator_strategy=strategy)
        )
    )
    record = AsyncMock()

    with patch.object(openai_compat, "_record_request_log", new=record):
        response = await confirm_draft(
            "video-draft",
            request,
            auth={"user_id": "user-1", "group_id": "group-1"},
        )

    assert response["video_id"] == "video-123"
    record.assert_awaited_once()
    assert record.call_args.kwargs["endpoint"] == (
        "/admin/draft/video-draft/confirm"
    )
    assert record.call_args.kwargs["model"] == "agnes-video-v2.0"


def test_video_submission_log_hashes_prompts_and_exposes_runtime_identity(monkeypatch):
    monkeypatch.setenv("AIGATEWAY_COMMIT_SHA", "commit-abc")
    draft = SimpleNamespace(
        draft_id="video-draft",
        workflow_version="workflow-fallback",
        generation_params={
            "request_id": "request-1",
            "trace_id": "trace-1",
            "source_draft_id": "image-draft",
            "source_image_sha256": "image-sha",
            "source_kind": "draft_result",
            "prompt_language": "zh",
            "keyframe_prompt": "一只黄白色柯基站在草地中央",
            "motion_prompt": "柯基摇尾巴并向镜头跑来",
            "duration_seconds": 5,
            "fps": 8,
            "frame_count": 41,
            "video_workflow_version": "wan22-v2",
        },
    )

    fields = video_submission_fields(draft, prompt_id="prompt-123")
    serialized = json.dumps(fields, ensure_ascii=False)

    assert fields["keyframe_prompt_hash"] == hashlib.sha256(
        "一只黄白色柯基站在草地中央".encode("utf-8")
    ).hexdigest()
    assert fields["motion_prompt_hash"] == hashlib.sha256(
        "柯基摇尾巴并向镜头跑来".encode("utf-8")
    ).hexdigest()
    assert fields["comfyui_prompt_id"] == "prompt-123"
    assert fields["deployed_commit_sha"] == "commit-abc"
    assert fields["input_image_name"] == "video-keyframe-video-draft.png"
    assert "一只黄白色柯基" not in serialized
    assert "柯基摇尾巴" not in serialized
