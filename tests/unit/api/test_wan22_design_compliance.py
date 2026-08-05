from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from aigateway_api.openai_compat import ChatCompletionRequest
from aigateway_api.trace_middleware import _validated_request_id
from aigateway_api.video_request_guard import reference_image_required
from aigateway_core.pipelines.generation._common.config import DraftWorkflowConfig
from aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError
from aigateway_core.pipelines.generation._common.models import (
    DRAFT_STATUS_CANCELLED,
    DRAFT_STATUS_RUNNING,
    DraftResult,
)
from aigateway_core.pipelines.generation.draft.draft_generator import (
    DraftGeneratorStrategy,
)
from aigateway_core.shared.integration_configs import ComfyUIConfig


@pytest.mark.parametrize(
    "prompt",
    [
        "以此图片生成5秒视频",
        "用当前图片做成视频",
        "让刚生成的图动起来",
        "拿这个结果生成动画",
        "Animate this image",
        "Create a video from the just-generated result",
    ],
)
def test_all_explicit_existing_image_references_fail_closed(prompt: str) -> None:
    body = SimpleNamespace(
        messages=[{"role": "user", "content": prompt}],
        generation_options={},
    )
    assert reference_image_required(body) is True


def test_source_draft_id_is_part_of_chat_generation_options_contract() -> None:
    body = ChatCompletionRequest.model_validate(
        {
            "model": "auto",
            "messages": [{"role": "user", "content": "让柯基跑向镜头"}],
            "chat_session_id": "session-1",
            "generation_options": {
                "backend": "local",
                "source_draft_id": "source-image",
                "duration_seconds": 5,
                "fps": 8,
            },
        }
    )
    assert body.generation_options is not None
    assert body.generation_options.source_draft_id == "source-image"


def test_request_identity_validation_rejects_header_injection() -> None:
    assert _validated_request_id("request-123:abc") == "request-123:abc"
    assert _validated_request_id("bad\r\nx-header: injected") == ""
    assert _validated_request_id("x" * 129) == ""


@pytest.fixture
def strategy(tmp_path) -> DraftGeneratorStrategy:
    config = DraftWorkflowConfig(
        enabled=True,
        store_dir=str(tmp_path / "drafts"),
        retention_period_hours=24,
    )
    return DraftGeneratorStrategy(
        config=config,
        redis_client=None,
        comfyui_config=ComfyUIConfig(
            workflow_version="image-v1",
            checkpoint_name="test-model.safetensors",
            allowed_checkpoints=["test-model.safetensors"],
            checkpoint_vram_gb={"test-model.safetensors": 8.0},
            sdxl_required_vram_gb=8.0,
        ),
    )


@pytest.mark.asyncio
async def test_cancel_request_stops_owned_background_task_and_persists_cancelled(
    strategy: DraftGeneratorStrategy,
) -> None:
    now = time.time()
    draft = DraftResult(
        draft_id="draft-1",
        previews=[],
        generation_params={"request_id": "request-1", "trace_id": "trace-1"},
        created_at=now,
        expires_at=now + 3600,
        status=DRAFT_STATUS_RUNNING,
        media_type="image",
        session_id="session-1",
        user_id="user-1",
        group_id=None,
        stage="running",
    )
    await strategy._store_draft(draft, 3600)
    await strategy.register_request_draft(
        "request-1",
        "draft-1",
        user_id="user-1",
        group_id=None,
        session_id="session-1",
        ttl_seconds=3600,
    )
    task = asyncio.create_task(asyncio.sleep(60), name="draft-generate-draft-1")
    strategy._bg_tasks.add(task)
    strategy._draft_tasks["draft-1"] = task

    cancelled = await strategy.cancel_request(
        "request-1",
        user_id="user-1",
        group_id=None,
        session_id="session-1",
    )

    assert task.done()
    assert cancelled is not None
    assert cancelled.status == DRAFT_STATUS_CANCELLED
    stored = await strategy.get_draft("draft-1")
    assert stored is not None
    assert stored.status == DRAFT_STATUS_CANCELLED
    assert stored.stage == "cancelled"


@pytest.mark.asyncio
async def test_cancel_request_fails_closed_for_wrong_owner(
    strategy: DraftGeneratorStrategy,
) -> None:
    await strategy.register_request_draft(
        "request-2",
        "draft-2",
        user_id="user-1",
        group_id=None,
        session_id="session-1",
        ttl_seconds=3600,
    )

    with pytest.raises(DraftWorkflowError, match="generation_request_forbidden"):
        await strategy.cancel_request(
            "request-2",
            user_id="user-2",
            group_id=None,
            session_id="session-1",
        )


@pytest.mark.asyncio
async def test_cancel_before_draft_registration_is_retained(
    strategy: DraftGeneratorStrategy,
) -> None:
    result = await strategy.cancel_request(
        "request-before-submit",
        user_id="user-1",
        group_id=None,
        session_id="session-1",
    )
    assert result is None
    record = await strategy._cancel_record("request-before-submit")
    assert record is not None
    assert record["user_id"] == "user-1"
    assert record["session_id"] == "session-1"
