"""ProviderCooldownTracker 单元测试。

测 tracker 的状态转换、per-model 独立性、provider 级聚合。
不依赖 litellm/网络/Redis。
"""

import time

from aigateway_core.route.bridge.cooldown import ProviderCooldownTracker


def test_closed_by_default():
    """新建 tracker,任意 model 查询状态默认 CLOSED(未记录)。"""
    tracker = ProviderCooldownTracker(allowed_fails=5, cooldown_time=60)
    assert tracker.get_all_status() == {}
    # provider 级也应为空
    assert tracker.get_provider_states() == {}


def test_failure_below_threshold_stays_closed():
    """失败次数低于阈值,状态仍是 CLOSED。"""
    tracker = ProviderCooldownTracker(allowed_fails=3, cooldown_time=60)
    tracker.on_failure("openai/gpt-4o")
    tracker.on_failure("openai/gpt-4o")
    status = tracker.get_all_status()["openai/gpt-4o"]
    assert status["state"] == "CLOSED"
    assert status["state_value"] == 0
    assert status["failure_count"] == 2


def test_failure_at_threshold_transitions_to_open():
    """连续失败达阈值,状态转 OPEN,cooldown_until 被设置。"""
    tracker = ProviderCooldownTracker(allowed_fails=3, cooldown_time=60)
    for _ in range(3):
        tracker.on_failure("openai/gpt-4o")
    status = tracker.get_all_status()["openai/gpt-4o"]
    assert status["state"] == "OPEN"
    assert status["state_value"] == 1
    assert status["cooldown_until"] is not None
    assert status["cooldown_until"] > time.time()  # cooldown 尚未过期


def test_success_resets_failure_count_in_closed():
    """CLOSED 状态下的 success 重置 failure_count。"""
    tracker = ProviderCooldownTracker(allowed_fails=5, cooldown_time=60)
    tracker.on_failure("openai/gpt-4o")
    tracker.on_failure("openai/gpt-4o")
    tracker.on_success("openai/gpt-4o")
    status = tracker.get_all_status()["openai/gpt-4o"]
    assert status["failure_count"] == 0
    assert status["state"] == "CLOSED"


def test_success_recovers_from_open():
    """OPEN 状态下的 success 转回 CLOSED,清空 failure_count 和 cooldown_until。"""
    tracker = ProviderCooldownTracker(allowed_fails=2, cooldown_time=60)
    tracker.on_failure("anthropic/claude-3")
    tracker.on_failure("anthropic/claude-3")
    assert tracker.get_all_status()["anthropic/claude-3"]["state"] == "OPEN"
    tracker.on_success("anthropic/claude-3")
    status = tracker.get_all_status()["anthropic/claude-3"]
    assert status["state"] == "CLOSED"
    assert status["failure_count"] == 0
    assert status["cooldown_until"] is None


def test_per_model_independent():
    """多个 model 状态互相独立。"""
    tracker = ProviderCooldownTracker(allowed_fails=2, cooldown_time=60)
    tracker.on_failure("openai/gpt-4o")
    tracker.on_failure("openai/gpt-4o")  # openai OPEN
    tracker.on_failure("anthropic/claude-3")  # anthropic 1 次失败,仍 CLOSED
    all_status = tracker.get_all_status()
    assert all_status["openai/gpt-4o"]["state"] == "OPEN"
    assert all_status["anthropic/claude-3"]["state"] == "CLOSED"
    assert all_status["anthropic/claude-3"]["failure_count"] == 1


def test_provider_states_aggregate_worst():
    """provider 级聚合:同 provider 任一 model OPEN → provider OPEN。"""
    tracker = ProviderCooldownTracker(allowed_fails=1, cooldown_time=60)
    # openai 有两个 model,一个 OPEN 一个 CLOSED
    tracker.on_failure("openai/gpt-4o")  # OPEN(allowed_fails=1)
    tracker.on_success("openai/gpt-3.5")  # CLOSED
    states = tracker.get_provider_states()
    assert states["openai"] == 1  # 聚合为 OPEN
