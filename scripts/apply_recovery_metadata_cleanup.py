from pathlib import Path

root = Path(__file__).resolve().parents[1]

impl = root / "aigateway-core/src/aigateway_core/pipelines/generation/draft/_draft_generator_impl.py"
text = impl.read_text(encoding="utf-8")
old = '''            draft.previews = previews
            draft.status = DRAFT_STATUS_PENDING
            draft.stage = "pending"
            draft.progress = 1.0
            await self._store_draft(draft, max(1, int(draft.expires_at - time.time())))
'''
new = '''            draft.previews = previews
            draft.status = DRAFT_STATUS_PENDING
            draft.stage = "pending"
            draft.progress = 1.0
            draft.generation_params.pop("recovery_error", None)
            draft.generation_params.pop("recovery_attempts", None)
            draft.generation_params.pop("last_recovery_error_type", None)
            await self._store_draft(draft, max(1, int(draft.expires_at - time.time())))
'''
if old not in text:
    raise RuntimeError("recovery success block not found")
impl.write_text(text.replace(old, new, 1), encoding="utf-8")

test_path = root / "tests/unit/pipeline/test_comfyui_recovery_errors.py"
test_text = test_path.read_text(encoding="utf-8")
addition = '''

@pytest.mark.asyncio
async def test_successful_recovery_clears_transient_retry_metadata(tmp_path):
    strategy = make_strategy(tmp_path)
    draft = make_running_draft("eventual-success")
    draft.generation_params.update(
        {
            "recovery_error": "comfyui_recovery_failed",
            "recovery_attempts": 2,
            "last_recovery_error_type": "ValueError",
        }
    )
    await strategy._store_draft(draft, ttl_seconds=3600)
    strategy._get_comfy_prompt_state = AsyncMock(return_value="completed")
    strategy._poll_results = AsyncMock(return_value=[b"preview"])

    synced = await strategy.sync_draft_runtime_state(draft.draft_id)

    assert synced is not None
    assert synced.status != DRAFT_STATUS_RUNNING
    assert synced.previews == [b"preview"]
    assert "recovery_error" not in synced.generation_params
    assert "recovery_attempts" not in synced.generation_params
    assert "last_recovery_error_type" not in synced.generation_params
'''
if "test_successful_recovery_clears_transient_retry_metadata" not in test_text:
    test_path.write_text(test_text.rstrip() + addition + "\n", encoding="utf-8")
