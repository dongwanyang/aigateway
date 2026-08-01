import time
from unittest.mock import AsyncMock

import pytest

from aigateway_core.pipelines.generation._common.config import DraftWorkflowConfig
from aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError
from aigateway_core.pipelines.generation._common.models import (
    DRAFT_STATUS_FAILED,
    DRAFT_STATUS_RUNNING,
    DraftResult,
)
from aigateway_core.pipelines.generation.draft.draft_generator import (
    DraftGeneratorStrategy,
)
from aigateway_core.shared.integration_configs import ComfyUIConfig


def make_strategy(tmp_path):
    config = DraftWorkflowConfig(store_dir=str(tmp_path / "drafts"))
    return DraftGeneratorStrategy(
        config=config,
        redis_client=None,
        comfyui_config=ComfyUIConfig(),
    )


def make_running_draft(code: str) -> DraftResult:
    return DraftResult(
        draft_id=code,
        previews=[],
        generation_params={"trace_id": f"trace-{code}"},
        created_at=time.time() - 120,
        expires_at=time.time() + 3600,
        attempt_number=1,
        max_attempts=5,
        status=DRAFT_STATUS_RUNNING,
        media_type="image",
        session_id=f"session-{code}",
        progress=0.5,
        stage="running",
        comfy_prompt_id=f"prompt-{code}",
    )


@pytest.mark.asyncio
async def test_runtime_recovery_preserves_comfyui_oom_root_cause(tmp_path):
    strategy = make_strategy(tmp_path)
    draft = make_running_draft("oom")
    await strategy._store_draft(draft, ttl_seconds=3600)
    strategy._get_comfy_prompt_state = AsyncMock(return_value="completed")
    strategy._poll_results = AsyncMock(
        side_effect=DraftWorkflowError("comfyui_gpu_out_of_memory")
    )

    synced = await strategy.sync_draft_runtime_state(draft.draft_id)

    assert synced is not None
    assert synced.status == DRAFT_STATUS_FAILED
    assert synced.error == "comfyui_gpu_out_of_memory"
    assert synced.stage == "comfyui_gpu_out_of_memory"
    assert synced.generation_params["recovery_error"] == "comfyui_recovery_failed"


@pytest.mark.asyncio
async def test_runtime_recovery_uses_generic_code_for_unknown_failures(tmp_path):
    strategy = make_strategy(tmp_path)
    draft = make_running_draft("unknown")
    await strategy._store_draft(draft, ttl_seconds=3600)
    strategy._get_comfy_prompt_state = AsyncMock(return_value="completed")
    strategy._poll_results = AsyncMock(side_effect=ValueError("invalid output"))

    synced = await strategy.sync_draft_runtime_state(draft.draft_id)

    assert synced is not None
    assert synced.status == DRAFT_STATUS_FAILED
    assert synced.error == "comfyui_recovery_failed"
