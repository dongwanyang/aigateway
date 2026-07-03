"""
Prompt 确认流程处理器
====================

当 AI Director 完成 prompt 优化后，根据配置决定是否需要用户确认：
- prompt_confirmation_enabled=True: 返回优化后 prompt 等待用户确认/编辑
- prompt_confirmation_enabled=False: 直接附加到请求元数据继续处理

用户确认流程:
1. 系统返回包含原始 prompt 和优化后 prompt 的确认响应
2. 用户选择: confirm（使用优化版）/ edit（提交编辑版）/ reject（使用原始版）
3. 系统将最终 prompt 附加到 Generation_Request 并继续管线处理

需求: 1.4
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

from aigateway_core.generation_optimization.config import AIDirectorConfig

logger = logging.getLogger(__name__)


class PromptConfirmationHandler:
    """Prompt 确认流程处理器.

    管理 AI Director 优化后的 prompt 确认流程，支持用户确认、编辑或拒绝优化结果。

    当 prompt_confirmation_enabled=True 时，暂停管线并返回确认响应给用户；
    当 prompt_confirmation_enabled=False 时，直接将优化后 prompt 附加到请求继续处理。

    Attributes:
        _config: AI Director 配置实例
        _pending_confirmations: 待确认的 prompt 映射（confirmation_id -> 确认数据）
    """

    def __init__(self, config: AIDirectorConfig) -> None:
        """初始化 Prompt 确认处理器.

        Args:
            config: AI Director 配置实例，用于读取 prompt_confirmation_enabled 开关
        """
        self._config = config
        # In-memory store for pending confirmations.
        # Key: confirmation_id, Value: dict with original_prompt, optimized_prompt, request_id
        self._pending_confirmations: Dict[str, Dict[str, str]] = {}

    def needs_confirmation(self) -> bool:
        """判断是否需要用户确认优化后的 prompt.

        Returns:
            True 如果 prompt_confirmation_enabled 配置为 True，否则 False
        """
        return self._config.prompt_confirmation_enabled

    def create_confirmation_response(
        self,
        original_prompt: str,
        optimized_prompt: str,
        request_id: str,
    ) -> Dict[str, Any]:
        """创建确认响应，包含原始和优化后的 prompt 供用户审核.

        生成唯一的 confirmation_id 并将待确认数据存入内存。
        返回的响应体包含两个 prompt 版本和确认 ID，供前端展示给用户。

        Args:
            original_prompt: 用户原始提示词
            optimized_prompt: AI Director 优化后的提示词
            request_id: 关联的请求 ID

        Returns:
            确认响应字典，包含:
            - confirmation_id: 确认流程唯一标识
            - request_id: 关联的请求 ID
            - status: "pending_confirmation"
            - original_prompt: 原始提示词
            - optimized_prompt: 优化后提示词
            - available_actions: 可用操作列表 ["confirm", "edit", "reject"]
        """
        confirmation_id = uuid.uuid4().hex

        # Store pending confirmation data
        self._pending_confirmations[confirmation_id] = {
            "original_prompt": original_prompt,
            "optimized_prompt": optimized_prompt,
            "request_id": request_id,
        }

        logger.info(
            "generation_optimization.prompt_confirmation.created",
            extra={
                "confirmation_id": confirmation_id,
                "request_id": request_id,
                "original_prompt_length": len(original_prompt),
                "optimized_prompt_length": len(optimized_prompt),
            },
        )

        return {
            "confirmation_id": confirmation_id,
            "request_id": request_id,
            "status": "pending_confirmation",
            "original_prompt": original_prompt,
            "optimized_prompt": optimized_prompt,
            "available_actions": ["confirm", "edit", "reject"],
        }

    def process_confirmation(
        self,
        confirmation_id: str,
        action: str,
        edited_prompt: Optional[str] = None,
    ) -> str:
        """处理用户的确认响应.

        根据用户选择的操作返回最终使用的 prompt:
        - action="confirm": 返回优化后的 prompt（用户接受优化结果）
        - action="edit": 返回用户编辑后的 prompt
        - action="reject": 返回原始 prompt（跳过优化）

        处理完成后从待确认列表中移除该条目。

        Args:
            confirmation_id: 确认流程唯一标识
            action: 用户操作 ("confirm" | "edit" | "reject")
            edited_prompt: 用户编辑后的 prompt（仅 action="edit" 时需要）

        Returns:
            最终确定使用的 prompt 文本

        Raises:
            KeyError: confirmation_id 不存在（已过期或无效）
            ValueError: action 不合法，或 action="edit" 时未提供 edited_prompt
        """
        if confirmation_id not in self._pending_confirmations:
            raise KeyError(
                f"Confirmation '{confirmation_id}' not found or already processed"
            )

        if action not in ("confirm", "edit", "reject"):
            raise ValueError(
                f"Invalid action '{action}'. Must be one of: confirm, edit, reject"
            )

        if action == "edit" and not edited_prompt:
            raise ValueError(
                "edited_prompt is required when action is 'edit'"
            )

        pending = self._pending_confirmations.pop(confirmation_id)
        request_id = pending["request_id"]

        if action == "confirm":
            final_prompt = pending["optimized_prompt"]
        elif action == "edit":
            # edited_prompt is guaranteed non-None here due to the check above
            final_prompt = edited_prompt  # type: ignore[assignment]
        else:  # action == "reject"
            final_prompt = pending["original_prompt"]

        logger.info(
            "generation_optimization.prompt_confirmation.processed",
            extra={
                "confirmation_id": confirmation_id,
                "request_id": request_id,
                "action": action,
                "final_prompt_length": len(final_prompt),
            },
        )

        return final_prompt

    def get_pending_confirmation(
        self, confirmation_id: str
    ) -> Optional[Dict[str, str]]:
        """查询待确认的 prompt 数据.

        Args:
            confirmation_id: 确认流程唯一标识

        Returns:
            待确认数据字典，如果不存在则返回 None
        """
        return self._pending_confirmations.get(confirmation_id)

    def cancel_pending(self, confirmation_id: str) -> bool:
        """取消一个待确认的 prompt（例如请求超时或用户取消）.

        Args:
            confirmation_id: 确认流程唯一标识

        Returns:
            True 如果成功取消，False 如果 confirmation_id 不存在
        """
        if confirmation_id in self._pending_confirmations:
            removed = self._pending_confirmations.pop(confirmation_id)
            logger.info(
                "generation_optimization.prompt_confirmation.cancelled",
                extra={
                    "confirmation_id": confirmation_id,
                    "request_id": removed["request_id"],
                },
            )
            return True
        return False

    @property
    def pending_count(self) -> int:
        """当前待确认的 prompt 数量."""
        return len(self._pending_confirmations)
