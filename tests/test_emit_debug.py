"""TraceCollector.emit_debug 按 5 维度开关控制 kind=debug 事件 + payload."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

import time
import aigateway_core.debug_config as dc
from aigateway_core.trace_event import TraceCollector, TraceEvent


class _FakeWatcher:
    def __init__(self, cfg):
        self._cfg = cfg
    @property
    def config(self):
        return self._cfg


def _set_debug(cfg):
    dc._watcher = _FakeWatcher(cfg)


def test_emit_debug_off_when_entry_disabled():
    TraceCollector._current.set(None)
    _set_debug(dc.DebugConfig(entry=False))
    c = TraceCollector.start("t-off")
    c.emit_debug("cache", "cache.get", 1.0, "ok", "entry", {"x": 1})
    assert len(c.events) == 0


def test_emit_debug_on_when_entry_enabled():
    TraceCollector._current.set(None)
    _set_debug(dc.DebugConfig(entry=True))
    c = TraceCollector.start("t-on")
    c.emit_debug("cache", "cache.get", 1.0, "ok", "entry", {"x": 1})
    assert len(c.events) == 1
    assert c.events[0].kind == "debug"
    assert c.events[0].payload == {"x": 1}
    assert c.events[0].status == "ok"
    dc._watcher = None


def test_emit_debug_plugin_and_logic():
    TraceCollector._current.set(None)
    # 总开关关 → 不发
    _set_debug(dc.DebugConfig(plugins_enabled=False, per_plugin={"pii_detector": True}))
    c = TraceCollector.start("t-and1")
    c.emit_debug("pii_detector", "pii.execute", 1.0, "ok", "plugin", {"k": "v"})
    assert len(c.events) == 0
    # 总开关开 + 单个开 → 发
    _set_debug(dc.DebugConfig(plugins_enabled=True, per_plugin={"pii_detector": True}))
    c2 = TraceCollector.start("t-and2")
    c2.emit_debug("pii_detector", "pii.execute", 1.0, "ok", "plugin", {"k": "v"})
    assert len(c2.events) == 1
    assert c2.events[0].payload == {"k": "v"}
    dc._watcher = None


def test_emit_debug_cache_bridge_dimensions():
    TraceCollector._current.set(None)
    _set_debug(dc.DebugConfig(cache=True, bridge=False))
    c = TraceCollector.start("t-cb")
    c.emit_debug("cache", "cache.get", 1.0, "ok", "cache", {"k": "v"})
    assert len(c.events) == 1
    c.emit_debug("bridge", "bridge.completion", 1.0, "ok", "bridge", {"k": "v"})
    assert len(c.events) == 1  # bridge 关,不追加
    dc._watcher = None


def test_emit_debug_unknown_dimension_silent():
    TraceCollector._current.set(None)
    _set_debug(dc.DebugConfig(entry=True))
    c = TraceCollector.start("t-unk")
    c.emit_debug("x", "y", 1.0, "ok", "nope", None)
    assert len(c.events) == 0
    dc._watcher = None
