"""Cost estimation for request metrics + quota accounting.

Moved verbatim from ``aigateway_api.openai_compat._estimate_cost`` (Task 5
runtime-structure refactor). The pricing table is kept in sync with
``aigateway_core.route.bridge.litellm_bridge.LiteLLMBridge._estimate_cost``.
"""
from __future__ import annotations


def _estimate_cost(model: str, total_tokens: int) -> float:
    """根据模型和 token 数估算成本（美元）。

    与 litellm_bridge._estimate_cost() 定价表保持一致。
    """
    pricing = {
        "gpt-4o": 0.000005,
        "gpt-4o-mini": 0.00000015,
        "claude-3-5-sonnet": 0.000003,
        "claude-3-haiku": 0.00000025,
        "gemini-1.5-pro": 0.0000025,
        "agnes-2.0-flash": 0.0000005,
    }
    base = model.split("/")[-1] if "/" in model else model
    return round(total_tokens * pricing.get(base, 0.000001), 6)
