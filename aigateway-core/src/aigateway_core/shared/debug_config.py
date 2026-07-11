"""5 维度 Debug 开关配置 + 热重载 watcher.

维度:
- frontend: control-panel 浏览器日志
- entry: auth + dispatcher + 共用前置 + quota + prompt_compress 内联
- cache: L1/L2/L3 CacheManager
- bridge: LiteLLMBridge + circuit breaker + auto 解析
- plugins: 插件层(总开关 + per_plugin AND 关系)

替代旧 debug_mode 总开关。走 ConfigManager.on_reload() 热重载,atomic swap 无锁读。
ConfigManager.on_reload 回调签名为 Callable[[Dict[str, Any]], None](接收整个新 config dict)。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class DebugConfig:
    frontend: bool = False
    entry: bool = False
    cache: bool = False
    bridge: bool = False
    plugins_enabled: bool = False
    per_plugin: dict[str, bool] = field(default_factory=dict)

    @classmethod
    def default(cls) -> "DebugConfig":
        return cls()

    @classmethod
    def from_yaml(cls, d: dict[str, Any] | None) -> "DebugConfig":
        """从 config.yaml 的 debug: 段构造(缺失返回默认).

        兼容两种写法:
        - 嵌套(官方 config.yaml):debug.plugins.enabled / debug.plugins.per_plugin
        - 扁平(admin /admin/config/debug 回显或前端发的):debug.plugins_enabled
        两者并存时嵌套优先(因为 config.yaml 原文用嵌套)。
        """
        if not d:
            return cls.default()
        plugins = d.get("plugins") or {}
        per_plugin = plugins.get("per_plugin") or {}
        # 扁平回退:某些 admin 路径(如 /admin/config/debug GET 回显)用 plugins_enabled
        plugins_enabled_nested = plugins.get("enabled")
        if plugins_enabled_nested is not None:
            plugins_enabled = bool(plugins_enabled_nested)
        else:
            plugins_enabled = bool(d.get("plugins_enabled", False))
        return cls(
            frontend=bool(d.get("frontend", False)),
            entry=bool(d.get("entry", False)),
            cache=bool(d.get("cache", False)),
            bridge=bool(d.get("bridge", False)),
            plugins_enabled=plugins_enabled,
            per_plugin={k: bool(v) for k, v in per_plugin.items()},
        )

    def is_plugin_debug(self, name: str) -> bool:
        """插件层 AND 逻辑:总开关 + 单个开关都开才生效."""
        return self.plugins_enabled and self.per_plugin.get(name, False)


class DebugConfigWatcher:
    """监听 ConfigManager 热重载,atomic swap DebugConfig.

    模式参照 GenerationOptimizationConfigWatcher。
    ConfigManager.on_reload() 回调签名为 Callable[[Dict], None],接收整个新 config dict,
    从中取 debug: 段构造新的 DebugConfig。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._config = DebugConfig.default()

    @property
    def config(self) -> DebugConfig:
        with self._lock:
            return self._config

    def attach(self, config_manager: Any) -> None:
        """注册到 ConfigManager.on_reload() 回调 + 立刻做一次首次加载."""
        if hasattr(config_manager, "on_reload"):
            config_manager.on_reload(self._on_config_reload)
        # 首次加载
        if hasattr(config_manager, "_config"):
            self._on_config_reload(config_manager._config)

    def _on_config_reload(self, new_full_config: Dict[str, Any]) -> None:
        """ConfigManager 触发的回调 —— 接收整个新 config dict,取 debug: 段."""
        raw = new_full_config.get("debug", {}) if isinstance(new_full_config, dict) else {}
        new_cfg = DebugConfig.from_yaml(raw)
        with self._lock:
            self._config = new_cfg


# 进程级单例(被 dispatcher/cache/bridge/pipeline 读取)
_watcher: "DebugConfigWatcher | None" = None


def get_debug_config() -> DebugConfig:
    """获取当前 DebugConfig(无 watcher 时返回 default)."""
    if _watcher is None:
        return DebugConfig.default()
    return _watcher.config


def init_debug_config_watcher(config_manager: Any) -> DebugConfigWatcher:
    """main.py 启动时调用一次,初始化并绑定进程级 watcher 单例."""
    global _watcher
    _watcher = DebugConfigWatcher()
    _watcher.attach(config_manager)
    return _watcher
