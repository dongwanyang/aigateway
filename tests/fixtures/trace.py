"""TraceEvent 拉取 + 顺序断言助手.

Spec §9 顺序断言策略:包含 + 相对顺序,不禁止中间插别的。
"""
import time
import pytest


def get_trace_events(admin_client, trace_id: str) -> list[dict]:
    """GET /admin/trace/{id} 返回 events 数组;不存在或限流返回 []."""
    r = admin_client.get(f"/admin/trace/{trace_id}")
    if r.status_code in (404, 429):
        return []
    r.raise_for_status()
    data = r.json()
    # Response 形如 {data: {events: [...], plugin_trace: [...], ...}, message: "success"}
    inner = data.get("data", data) if isinstance(data, dict) else data
    return inner.get("events", []) if isinstance(inner, dict) else []


def wait_for_trace(admin_client, trace_id: str, timeout: float = 5.0) -> list[dict]:
    """轮询直到 trace 落 redis,或超时。

    Admin `/admin/*` 有 IP 级速率限制 30 req/60s。这里用 1.0s 的间隔
    (最多 timeout 秒 poll timeout+1 次)避免撑爆 rate limiter。
    """
    deadline = time.time() + timeout
    interval = 1.0
    while time.time() < deadline:
        evs = get_trace_events(admin_client, trace_id)
        if evs:
            return evs
        time.sleep(interval)
    # 最后再试一次
    return get_trace_events(admin_client, trace_id)


def filter_events(events: list[dict], **matchers) -> list[dict]:
    """按 kind/name/dimension/stage/status 精确过滤."""
    def match(e):
        return all(e.get(k) == v for k, v in matchers.items())
    return [e for e in events if match(e)]


def assert_events_order(events: list[dict], must_contain_in_order: list[str]) -> None:
    """按 e['name'] 断言给定 name 全部存在且相对顺序正确.

    存在多个同名 event 时取首次出现位置。中间允许插入其他 name(不检查)。
    """
    names = [e.get("name", "") for e in events]
    positions = []
    for target in must_contain_in_order:
        try:
            positions.append(names.index(target))
        except ValueError:
            raise AssertionError(
                f"Expected event name {target!r} not in trace. "
                f"Got names: {names}"
            )
    if positions != sorted(positions):
        raise AssertionError(
            f"Events out of order. Expected {must_contain_in_order}, "
            f"got positions {positions} in {names}"
        )


@pytest.fixture
def trace_helpers(admin_client):
    """Bundle the helpers as attrs on a namespace for convenient tests use."""
    class TH:
        get = staticmethod(lambda tid: get_trace_events(admin_client, tid))
        wait = staticmethod(lambda tid, timeout=5.0: wait_for_trace(admin_client, tid, timeout))
        filter = staticmethod(filter_events)
        assert_order = staticmethod(assert_events_order)
    return TH
