"""
Tests for PromptConfirmationHandler — Prompt 确认流程
=====================================================

验证:
- needs_confirmation(): 根据配置返回正确的布尔值
- create_confirmation_response(): 创建包含正确数据的确认响应
- process_confirmation(): 不同 action 返回正确的 prompt
  - confirm: 返回优化后 prompt
  - edit: 返回用户编辑后的 prompt
  - reject: 返回原始 prompt
- 错误处理: 无效 confirmation_id、无效 action、缺失 edited_prompt
- 确认后从 pending 列表移除
- cancel_pending(): 取消待确认项

需求: 1.4
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.generation_optimization.config import AIDirectorConfig
from aigateway_core.generation_optimization.strategies.prompt_confirmation import (
    PromptConfirmationHandler,
)


@pytest.fixture
def config_enabled():
    """Config with prompt confirmation enabled."""
    return AIDirectorConfig(prompt_confirmation_enabled=True)


@pytest.fixture
def config_disabled():
    """Config with prompt confirmation disabled."""
    return AIDirectorConfig(prompt_confirmation_enabled=False)


@pytest.fixture
def handler_enabled(config_enabled):
    """Handler with confirmation enabled."""
    return PromptConfirmationHandler(config=config_enabled)


@pytest.fixture
def handler_disabled(config_disabled):
    """Handler with confirmation disabled."""
    return PromptConfirmationHandler(config=config_disabled)


class TestNeedsConfirmation:
    """Tests for needs_confirmation()."""

    def test_returns_true_when_enabled(self, handler_enabled):
        """Should return True when prompt_confirmation_enabled is True."""
        assert handler_enabled.needs_confirmation() is True

    def test_returns_false_when_disabled(self, handler_disabled):
        """Should return False when prompt_confirmation_enabled is False."""
        assert handler_disabled.needs_confirmation() is False


class TestCreateConfirmationResponse:
    """Tests for create_confirmation_response()."""

    def test_creates_response_with_correct_fields(self, handler_enabled):
        """Response should contain all required fields."""
        response = handler_enabled.create_confirmation_response(
            original_prompt="a cat",
            optimized_prompt="【主体】一只猫咪\n【动作】安静地坐着",
            request_id="req-001",
        )

        assert "confirmation_id" in response
        assert response["request_id"] == "req-001"
        assert response["status"] == "pending_confirmation"
        assert response["original_prompt"] == "a cat"
        assert response["optimized_prompt"] == "【主体】一只猫咪\n【动作】安静地坐着"
        assert response["available_actions"] == ["confirm", "edit", "reject"]

    def test_confirmation_id_is_unique(self, handler_enabled):
        """Each call should produce a unique confirmation_id."""
        r1 = handler_enabled.create_confirmation_response("a", "b", "req-1")
        r2 = handler_enabled.create_confirmation_response("c", "d", "req-2")
        assert r1["confirmation_id"] != r2["confirmation_id"]

    def test_stores_pending_confirmation(self, handler_enabled):
        """Should store the confirmation data internally."""
        response = handler_enabled.create_confirmation_response(
            original_prompt="hello",
            optimized_prompt="optimized hello",
            request_id="req-100",
        )

        pending = handler_enabled.get_pending_confirmation(
            response["confirmation_id"]
        )
        assert pending is not None
        assert pending["original_prompt"] == "hello"
        assert pending["optimized_prompt"] == "optimized hello"
        assert pending["request_id"] == "req-100"

    def test_pending_count_increments(self, handler_enabled):
        """pending_count should track number of pending confirmations."""
        assert handler_enabled.pending_count == 0
        handler_enabled.create_confirmation_response("a", "b", "r1")
        assert handler_enabled.pending_count == 1
        handler_enabled.create_confirmation_response("c", "d", "r2")
        assert handler_enabled.pending_count == 2


class TestProcessConfirmation:
    """Tests for process_confirmation()."""

    def test_confirm_returns_optimized_prompt(self, handler_enabled):
        """action='confirm' should return the optimized prompt."""
        response = handler_enabled.create_confirmation_response(
            original_prompt="original",
            optimized_prompt="optimized version",
            request_id="req-001",
        )

        result = handler_enabled.process_confirmation(
            confirmation_id=response["confirmation_id"],
            action="confirm",
        )

        assert result == "optimized version"

    def test_edit_returns_edited_prompt(self, handler_enabled):
        """action='edit' should return the user's edited prompt."""
        response = handler_enabled.create_confirmation_response(
            original_prompt="original",
            optimized_prompt="optimized",
            request_id="req-002",
        )

        result = handler_enabled.process_confirmation(
            confirmation_id=response["confirmation_id"],
            action="edit",
            edited_prompt="my custom version",
        )

        assert result == "my custom version"

    def test_reject_returns_original_prompt(self, handler_enabled):
        """action='reject' should return the original prompt."""
        response = handler_enabled.create_confirmation_response(
            original_prompt="my original prompt",
            optimized_prompt="some optimized prompt",
            request_id="req-003",
        )

        result = handler_enabled.process_confirmation(
            confirmation_id=response["confirmation_id"],
            action="reject",
        )

        assert result == "my original prompt"

    def test_removes_from_pending_after_processing(self, handler_enabled):
        """Confirmation should be removed from pending after processing."""
        response = handler_enabled.create_confirmation_response(
            original_prompt="a", optimized_prompt="b", request_id="r1"
        )
        cid = response["confirmation_id"]

        assert handler_enabled.pending_count == 1
        handler_enabled.process_confirmation(cid, action="confirm")
        assert handler_enabled.pending_count == 0
        assert handler_enabled.get_pending_confirmation(cid) is None

    def test_invalid_confirmation_id_raises_key_error(self, handler_enabled):
        """Non-existent confirmation_id should raise KeyError."""
        with pytest.raises(KeyError, match="not found"):
            handler_enabled.process_confirmation(
                confirmation_id="nonexistent-id",
                action="confirm",
            )

    def test_already_processed_raises_key_error(self, handler_enabled):
        """Processing the same confirmation twice should raise KeyError."""
        response = handler_enabled.create_confirmation_response(
            original_prompt="a", optimized_prompt="b", request_id="r1"
        )
        cid = response["confirmation_id"]

        handler_enabled.process_confirmation(cid, action="confirm")

        with pytest.raises(KeyError, match="not found"):
            handler_enabled.process_confirmation(cid, action="confirm")

    def test_invalid_action_raises_value_error(self, handler_enabled):
        """Invalid action should raise ValueError."""
        response = handler_enabled.create_confirmation_response(
            original_prompt="a", optimized_prompt="b", request_id="r1"
        )

        with pytest.raises(ValueError, match="Invalid action"):
            handler_enabled.process_confirmation(
                confirmation_id=response["confirmation_id"],
                action="invalid_action",
            )

    def test_edit_without_edited_prompt_raises_value_error(self, handler_enabled):
        """action='edit' without edited_prompt should raise ValueError."""
        response = handler_enabled.create_confirmation_response(
            original_prompt="a", optimized_prompt="b", request_id="r1"
        )

        with pytest.raises(ValueError, match="edited_prompt is required"):
            handler_enabled.process_confirmation(
                confirmation_id=response["confirmation_id"],
                action="edit",
                edited_prompt=None,
            )

    def test_edit_with_empty_string_raises_value_error(self, handler_enabled):
        """action='edit' with empty string should raise ValueError."""
        response = handler_enabled.create_confirmation_response(
            original_prompt="a", optimized_prompt="b", request_id="r1"
        )

        with pytest.raises(ValueError, match="edited_prompt is required"):
            handler_enabled.process_confirmation(
                confirmation_id=response["confirmation_id"],
                action="edit",
                edited_prompt="",
            )


class TestCancelPending:
    """Tests for cancel_pending()."""

    def test_cancel_existing_returns_true(self, handler_enabled):
        """Cancelling an existing confirmation should return True."""
        response = handler_enabled.create_confirmation_response(
            original_prompt="a", optimized_prompt="b", request_id="r1"
        )

        result = handler_enabled.cancel_pending(response["confirmation_id"])
        assert result is True
        assert handler_enabled.pending_count == 0

    def test_cancel_nonexistent_returns_false(self, handler_enabled):
        """Cancelling a non-existent confirmation should return False."""
        result = handler_enabled.cancel_pending("nonexistent-id")
        assert result is False


class TestConfirmationDisabledFlow:
    """Integration-style tests for the disabled confirmation flow."""

    def test_disabled_flow_skips_confirmation(self, handler_disabled):
        """When disabled, needs_confirmation returns False - caller should
        attach optimized prompt directly without creating a confirmation."""
        assert handler_disabled.needs_confirmation() is False

        # Simulate the pipeline: when disabled, we don't create a confirmation.
        # The optimized prompt is attached directly to the request.
        optimized = "optimized prompt text"

        # No confirmation needed, use optimized directly
        final_prompt = optimized
        assert final_prompt == "optimized prompt text"
        assert handler_disabled.pending_count == 0
