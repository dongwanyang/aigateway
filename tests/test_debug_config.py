"""DebugConfig 单元测试 —— 5 维度 + AND 逻辑."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.shared.debug_config import DebugConfig, DebugConfigWatcher


def test_default_all_off():
    c = DebugConfig.default()
    assert c.frontend is False
    assert c.entry is False
    assert c.cache is False
    assert c.bridge is False
    assert c.plugins_enabled is False
    assert c.per_plugin == {}


def test_from_yaml_full():
    d = {
        "frontend": True, "entry": False, "cache": True, "bridge": False,
        "plugins": {"enabled": True, "per_plugin": {"pii_detector": True, "rag_retriever": False}},
    }
    c = DebugConfig.from_yaml(d)
    assert c.frontend is True
    assert c.cache is True
    assert c.plugins_enabled is True
    assert c.per_plugin == {"pii_detector": True, "rag_retriever": False}


def test_is_plugin_debug_and_logic():
    # 总开关关 → 即使单个开也不生效
    c = DebugConfig(plugins_enabled=False, per_plugin={"pii_detector": True})
    assert c.is_plugin_debug("pii_detector") is False
    # 总开关开 + 单个开 → 生效
    c = DebugConfig(plugins_enabled=True, per_plugin={"pii_detector": True})
    assert c.is_plugin_debug("pii_detector") is True
    # 总开关开 + 单个关 → 不生效
    c = DebugConfig(plugins_enabled=True, per_plugin={"pii_detector": False})
    assert c.is_plugin_debug("pii_detector") is False
    # 未列出的插件 → 不生效
    c = DebugConfig(plugins_enabled=True, per_plugin={"pii_detector": True})
    assert c.is_plugin_debug("unknown") is False


def test_from_yaml_missing_section():
    """config.yaml 没有 debug: 段时,返回全部关闭的默认值."""
    c = DebugConfig.from_yaml({})
    assert c == DebugConfig.default()
    c2 = DebugConfig.from_yaml(None)
    assert c2 == DebugConfig.default()


def test_watcher_attach_loads_initial_config():
    """DebugConfigWatcher.attach 应立刻从 config_manager.config 做一次首次加载."""
    class FakeCM:
        def __init__(self):
            self._config = {"debug": {"frontend": True, "cache": True}}
            self._callbacks = []
        def on_reload(self, cb):
            self._callbacks.append(cb)

    cm = FakeCM()
    w = DebugConfigWatcher()
    w.attach(cm)
    assert w.config.frontend is True
    assert w.config.cache is True
    assert w.config.bridge is False


def test_watcher_reload_swaps_atomically():
    """ConfigManager 触发的 on_reload 回调应更新 watcher.config."""
    class FakeCM:
        def __init__(self):
            self.config = {"debug": {}}
            self._callbacks = []
        def on_reload(self, cb):
            self._callbacks.append(cb)
        def fire_reload(self, new_config):
            for cb in self._callbacks:
                cb(new_config)

    cm = FakeCM()
    w = DebugConfigWatcher()
    w.attach(cm)
    assert w.config.entry is False

    cm.fire_reload({"debug": {"entry": True, "plugins": {"enabled": True, "per_plugin": {"pii_detector": True}}}})
    assert w.config.entry is True
    assert w.config.is_plugin_debug("pii_detector") is True
