"""ProviderCooldownTracker — per-model cooldown state mirror.

Part of the unified route layer (``aigateway_core.route.bridge``).

Moved here from the root ``aigateway_core/litellm_bridge.py`` as part of the
runtime structure refactor (Task 4). Behavior is unchanged.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict

logger = logging.getLogger(__name__)


class ProviderCooldownTracker:
    """per-model cooldown 状态跟踪器。

    由 LiteLLMBridge 通过 litellm Router 的 deployment callback 驱动。
    litellm 内部自己也有 cooldown(_filter_cooldown_deployments),这里
    维护一份镜像供 /metrics 与 admin 同步读取(避免每次 /metrics 请求
    调 litellm async API)。

    状态:CLOSED(0)/ OPEN(1),不实现 HALF-OPEN(litellm 无对应概念)。
    """

    def __init__(
        self,
        allowed_fails: int = 5,
        cooldown_time: int = 60,
        long_open_alert_seconds: int = 300,
    ) -> None:
        import threading
        self.allowed_fails = allowed_fails
        self.cooldown_time = cooldown_time
        self.long_open_alert_seconds = long_open_alert_seconds
        # {model_name: {"state": "CLOSED"/"OPEN", "failure_count": int,
        #               "last_failure_time": float, "last_success_time": float,
        #               "cooldown_until": float|None}}
        self._models: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _extract_provider(model: str) -> str:
        """从 model 名提取 provider,如 openai/gpt-4o → openai。"""
        if "/" in model:
            return model.split("/", 1)[0]
        return model

    def _get_or_init(self, model: str) -> Dict[str, Any]:
        if model not in self._models:
            self._models[model] = {
                "state": "CLOSED",
                "failure_count": 0,
                "last_failure_time": 0.0,
                "last_success_time": 0.0,
                "cooldown_until": None,
            }
        return self._models[model]

    def on_failure(self, model: str) -> None:
        """记一次失败;累计达 allowed_fails → 转 OPEN。"""
        if not model:
            return
        with self._lock:
            entry = self._get_or_init(model)
            entry["failure_count"] += 1
            entry["last_failure_time"] = time.time()
            if entry["state"] == "CLOSED" and entry["failure_count"] >= self.allowed_fails:
                entry["state"] = "OPEN"
                entry["cooldown_until"] = time.time() + self.cooldown_time
                logger.warning(
                    "cooldown: model=%s → OPEN(连续失败 %d 次,cooldown %ds)",
                    model, entry["failure_count"], self.cooldown_time,
                )
            # long_open 告警:本次失败发生时如果已 OPEN 且时间过长,输出一次 error
            if entry["state"] == "OPEN" and entry["cooldown_until"]:
                open_duration = time.time() - (entry["cooldown_until"] - self.cooldown_time)
                if open_duration >= self.long_open_alert_seconds:
                    logger.error(
                        "cooldown alert: model=%s OPEN 持续 %.0fs 超过阈值 %ds",
                        model, open_duration, self.long_open_alert_seconds,
                    )

    def on_success(self, model: str) -> None:
        """记一次成功;OPEN → CLOSED,或 CLOSED 状态下重置 failure_count。"""
        if not model:
            return
        with self._lock:
            entry = self._get_or_init(model)
            entry["last_success_time"] = time.time()
            if entry["state"] == "OPEN":
                logger.info("cooldown: model=%s → CLOSED(恢复正常)", model)
            entry["state"] = "CLOSED"
            entry["failure_count"] = 0
            entry["cooldown_until"] = None

    def get_all_status(self) -> Dict[str, Dict[str, Any]]:
        """返回所有 model 状态的浅拷贝(供 /admin/health 读)。"""
        with self._lock:
            return {
                m: {
                    "state": e["state"],
                    "state_value": 0 if e["state"] == "CLOSED" else 1,
                    "failure_count": e["failure_count"],
                    "last_failure_time": e["last_failure_time"],
                    "last_success_time": e["last_success_time"],
                    "cooldown_until": e["cooldown_until"],
                }
                for m, e in self._models.items()
            }

    def get_provider_states(self) -> Dict[str, int]:
        """按 provider 聚合状态,任一 model OPEN → provider OPEN。

        供 /metrics 上报 Prometheus circuit_breaker_state gauge。
        """
        with self._lock:
            provider_state: Dict[str, int] = {}
            for m, e in self._models.items():
                p = self._extract_provider(m)
                v = 0 if e["state"] == "CLOSED" else 1
                cur = provider_state.get(p)
                if cur is None or v > cur:
                    provider_state[p] = v
            return provider_state
