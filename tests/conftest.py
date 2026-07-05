"""共享 pytest fixtures.

autouse 重置 TraceCollector._current —— 防止用例间 ContextVar 残留。
各测试用 TraceCollector.start() 启动自己的 collector,但用例结束后
_current 不会自动清空,可能让后续未启动 collector 的用例看到陈旧值。
此 fixture 在每个用例前重置,保证隔离。
"""
import pytest


@pytest.fixture(autouse=True)
def _reset_trace_collector():
    """每个测试前重置 TraceCollector ContextVar,防止跨用例泄漏."""
    from aigateway_core.trace_event import TraceCollector
    TraceCollector._current.set(None)
    yield
    TraceCollector._current.set(None)
