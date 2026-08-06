from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aigateway_api.draft_confirm_routes import confirm_draft
from aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError
from aigateway_core.pipelines.generation._common.image_reference import (
    latest_user_text,
    missing_required_image_reference,
)
from aigateway_core.pipelines.generation._common.models import VideoSubmitResult
from aigateway_core.pipelines.generation._common.video_observability import (
    video_submission_fields,
)
from fastapi import HTTPException


def _guard(content, *, pipeline_kind="generation:video", source_draft_id=None):
    """Evaluate the core reference rule the way the dispatcher does."""
    messages = [{"role": "user", "content": content}]
    urls = [
        part["image_url"]["url"]
        for part in (content if isinstance(content, list) else [])
        if isinstance(part, dict) and part.get("type") == "image_url"
    ]
    return missing_required_image_reference(
        pipeline_kind=pipeline_kind,
        prompt_text=latest_user_text(messages),
        reference_image_urls=urls,
        source_draft_id=source_draft_id,
    )


@pytest.mark.parametrize(
    "content",
    [
        "根据这张图生成一个 5 秒视频",
        # Phrasings the previous regex-based HTTP guard silently let through.
        "以此图片生成5秒视频",
        "此图生成视频",
        "用上面的图生成视频",
        "刚才那张图做成视频",
        "上传的图生成视频",
        "animate the attached screenshot",
    ],
)
def test_missing_anaphoric_video_reference_is_rejected(content):
    assert _guard(content) is True


@pytest.mark.parametrize(
    "content",
    [
        "请解释这张图片和视频的区别",
        "这张图片适合做视频封面吗",
        "What is the difference between this image and a video?",
    ],
)
def test_non_generation_image_video_questions_are_not_rejected(content):
    # Intent comes from the classifier, not from prompt text: these are answered
    # by the understanding pipeline and must never be rejected.
    assert _guard(content, pipeline_kind="understanding") is False


@pytest.mark.parametrize(
    "content",
    [
        "生成一只柯基摇尾巴向镜头跑来，5秒",
        "生成视频：画面里有一只猫",
        "make a 5 second video of a corgi running",
    ],
)
def test_text_to_video_without_anaphora_is_allowed(content):
    assert _guard(content) is False


def test_uploaded_image_or_source_draft_satisfies_reference_requirement():
    uploaded = [
        {"type": "text", "text": "让这张图动起来"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,aW1hZ2U="},
        },
    ]

    assert _guard(uploaded) is False
    assert _guard("根据这张图生成视频", source_draft_id="image-draft") is False


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
        "一只黄白色柯基站在草地中央".encode()
    ).hexdigest()
    assert fields["motion_prompt_hash"] == hashlib.sha256(
        "柯基摇尾巴并向镜头跑来".encode()
    ).hexdigest()
    assert fields["comfyui_prompt_id"] == "prompt-123"
    assert fields["deployed_commit_sha"] == "commit-abc"
    assert fields["input_image_name"] == "video-keyframe-video-draft.png"
    assert "一只黄白色柯基" not in serialized
    assert "柯基摇尾巴" not in serialized
