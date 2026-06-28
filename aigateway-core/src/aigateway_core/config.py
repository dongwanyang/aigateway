"""
ConfigManager — YAML 配置加载器
===============================

支持：
- YAML 文件解析
- 环境变量覆盖（AI_GATEWAY_* 前缀）
- Watchdog 文件监听实现热重载
- 原子交换（CAS）确保并发安全

根据 TECH_SPEC.md config.yaml 完整 Schema 和环境变量定义。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import yaml

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# 环境变量到配置路径的映射
# 环境变量名格式: AI_GATEWAY_<SECTION>_<KEY>（大写，下划线分隔）
# ------------------------------------------------------------------

# 各配置节的默认值（作为 schema 参考）
_DEFAULT_CONFIG: Dict[str, Any] = {
    "server": {
        "host": "0.0.0.0",
        "port": 8000,
        "workers": 4,
    },
    "auth": {
        "distributed_mode": False,
        "api_keys": [],
    },
    "plugins": [],
    "providers": {},
    "embedding": {
        "backend": "sentence_transformers",
        "model": "all-MiniLM-L6-v2",
        "openai_model": "text-embedding-3-small",
    },
    "observability": {
        "prometheus_enabled": True,
        "opentelemetry_enabled": True,
        "otel_service_name": "ai-gateway",
        "otel_sample_rate": 0.1,
        "log_format": "json",
        "log_level": "info",
    },
}


class ConfigManager:
    """YAML 配置管理器。

    提供配置加载、环境变量覆盖、文件监听热重载和原子交换能力。

    属性:
        config_path: 配置文件路径。
        _config: 当前生效的配置字典（深拷贝，线程安全）。
        _lock: 线程锁，用于原子交换。
        _watchdog: Watchdog 文件系统事件处理器（可选）。
        _reload_callbacks: 热重载回调函数列表。
    """

    def __init__(self, config_path: Optional[str] = None) -> None:
        """
        Args:
            config_path: 配置文件路径，默认从 AI_GATEWAY_CONFIG_PATH 读取，
                        再缺省用 "./config.yaml"。
        """
        # 确定配置文件路径
        self.config_path = config_path or os.environ.get(
            "AI_GATEWAY_CONFIG_PATH", "./config.yaml"
        )

        self._config: Dict[str, Any] = {}
        self._lock = threading.RLock()
        self._reload_callbacks: list[Callable[[Dict[str, Any]], None]] = []
        self._watchdog: Any = None
        self._watch_handle: Any = None
        self._watchdog_active = False

        # 加载配置
        self.load()

    # ------------------------------------------------------------------
    # 配置加载
    # ------------------------------------------------------------------

    def load(self) -> Dict[str, Any]:
        """从 YAML 文件加载配置，并应用环境变量覆盖。

        Returns:
            合并后的配置字典。
        """
        config = self._load_yaml(self.config_path)
        config = self._apply_env_overrides(config)
        config = self._resolve_env_vars_in_values(config)

        with self._lock:
            self._config = copy.deepcopy(config)

        logger.info(
            "配置已加载: path=%s, keys=%s",
            self.config_path,
            list(config.keys()),
        )
        return self._config

    def _load_yaml(self, path: str) -> Dict[str, Any]:
        """解析 YAML 文件。

        Args:
            path: 文件路径。

        Returns:
            配置字典。若文件不存在则返回空字典。
        """
        filepath = Path(path)
        if not filepath.exists():
            logger.warning("配置文件不存在，使用空配置: %s", path)
            return {}

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if not isinstance(data, dict):
                logger.error("配置文件格式错误: 顶层必须为 YAML 对象")
                return {}
            return data
        except yaml.YAMLError as exc:
            logger.error("YAML 解析失败: %s", exc)
            return {}
        except OSError as exc:
            logger.error("读取配置文件失败: %s", exc)
            return {}

    def _apply_env_overrides(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """将环境变量（AI_GATEWAY_*）覆盖到配置中。

        环境变量命名规则:
        - AI_GATEWAY_SERVER_HOST -> config.server.host
        - AI_GATEWAY_SERVER_PORT -> config.server.port (自动转为 int)
        - AI_GATEWAY_OBSERVABILITY_LOG_LEVEL -> config.observability.log_level

        Args:
            config: 原始配置字典。

        Returns:
            应用覆盖后的配置字典。
        """
        env_prefix = "AI_GATEWAY_"
        env_keys = sorted(
            k for k in os.environ if k.startswith(env_prefix)
        )

        for env_key in env_keys:
            # 去掉前缀，转为小写下划线路径
            config_path = env_key[len(env_prefix):].lower()
            value = os.environ[env_key]

            # 类型推断：尝试解析为数字、布尔值或 JSON 值
            parsed_value = self._parse_env_value(value)

            # 将路径拆分为层级，逐层设置
            self._set_nested(config, config_path, parsed_value)

        if env_keys:
            logger.info(
                "环境变量覆盖: %d 个变量应用到配置",
                len(env_keys),
            )

        return config

    def _resolve_env_vars_in_values(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """递归解析配置值中的 ${ENV_VAR} 引用。

        例如: api_key: "${OPENAI_API_KEY}" 会被替换为实际值。

        Args:
            config: 配置字典。

        Returns:
            变量替换后的配置字典。
        """
        return self._resolve_recursive(config)

    def _resolve_recursive(self, data: Any) -> Any:
        """递归遍历并替换字符串中的环境变量引用。

        Args:
            data: 任意类型的配置值。

        Returns:
            替换后的值。
        """
        if isinstance(data, str):
            # 匹配 ${VAR_NAME} 或 ${VAR_NAME:-default}
            import re
            pattern = r"\$\{([^}]+)\}"

            def replacer(match: Any) -> str:
                expr = match.group(1)
                # 处理默认值语法 ${VAR:-default}
                if ":-" in expr:
                    var_name, default_val = expr.split(":-", 1)
                    return os.environ.get(var_name.strip(), default_val)
                return os.environ.get(expr.strip(), match.group(0))

            return re.sub(pattern, replacer, data)
        elif isinstance(data, dict):
            return {k: self._resolve_recursive(v) for k, v in data.items()}
        elif isinstance(data, list):
            return [self._resolve_recursive(item) for item in data]
        else:
            return data

    @staticmethod
    def _set_nested(
        config: Dict[str, Any], dotted_path: str, value: Any
    ) -> None:
        """在嵌套字典中按点分隔路径设置值。

        Args:
            config: 目标配置字典。
            dotted_path: 点分隔的路径，如 "server.host"。
            value: 要设置的值。
        """
        keys = dotted_path.split(".")
        current = config
        for key in keys[:-1]:
            if key not in current:
                current[key] = {}
            current = current[key]
        current[keys[-1]] = value

    @staticmethod
    def _parse_env_value(value: str) -> Any:
        """尝试将环境变量字符串解析为合适的类型。

        解析优先级: JSON 值 > 布尔值 > 整数 > 浮点数 > 字符串

        Args:
            value: 环境变量原始字符串值。

        Returns:
            解析后的值。
        """
        # 尝试 JSON 解析（数组、对象、数字等）
        try:
            parsed = json.loads(value)
            return parsed
        except (json.JSONDecodeError, ValueError):
            pass

        # 布尔值
        if value.lower() in ("true", "yes", "1"):
            return True
        if value.lower() in ("false", "no", "0"):
            return False

        # 整数
        try:
            return int(value)
        except ValueError:
            pass

        # 浮点数
        try:
            return float(value)
        except ValueError:
            pass

        # 回退为字符串
        return value

    # ------------------------------------------------------------------
    # 配置读写
    # ------------------------------------------------------------------

    def get(self, path: str, default: Any = None) -> Any:
        """按点分隔路径读取配置值。

        Args:
            path: 点分隔路径，如 "server.host"。
            default: 路径不存在时的默认值。

        Returns:
            配置值或 default。
        """
        keys = path.split(".")
        current: Any = self._config

        with self._lock:
            for key in keys:
                if isinstance(current, dict):
                    current = current.get(key)
                else:
                    return default
                if current is None:
                    return default

        return current

    def set(self, path: str, value: Any) -> None:
        """按点分隔路径设置配置值（运行时动态修改）。

        Args:
            path: 点分隔路径，如 "server.port"。
            value: 要设置的值。
        """
        keys = path.split(".")
        with self._lock:
            config = self._config
            for key in keys[:-1]:
                if key not in config or not isinstance(config[key], dict):
                    config[key] = {}
                config = config[key]
            config[keys[-1]] = value

        logger.info("配置已更新: %s = %s", path, value)

    def snapshot(self) -> Dict[str, Any]:
        """获取当前配置的深拷贝（线程安全）。

        Returns:
            配置字典副本。
        """
        with self._lock:
            return copy.deepcopy(self._config)

    # ------------------------------------------------------------------
    # 原子交换
    # ------------------------------------------------------------------

    def atomic_swap(self, new_config: Dict[str, Any]) -> bool:
        """原子交换整个配置。

        在锁保护下将新配置深度替换旧配置，确保并发安全。
        适用于 YAML 文件变更后重新加载。

        Args:
            new_config: 新的配置字典。

        Returns:
            是否交换成功。
        """
        with self._lock:
            old_config = copy.deepcopy(self._config)
            self._config = copy.deepcopy(new_config)

        # 触发热重载回调
        self._notify_reload(old_config, new_config)
        logger.info("配置原子交换完成")
        return True

    def save(self, path: str, value: Any) -> bool:
        """保存配置值到路径并写回 YAML 文件。

        Args:
            path: 点分隔路径，如 "plugins"。
            value: 要保存的值。

        Returns:
            是否保存成功。
        """
        try:
            with self._lock:
                self._set_nested(self._config, path, value)
                self._write_yaml()
                # 写回后立即从文件重新加载，确保内存与磁盘一致
                # 多 worker 场景下，其他 worker 的内存可能过期，
                # 热重载（Watchdog）或下次请求时重新加载可保证一致性
                self._config = self._load_yaml(self.config_path)
                # 重新应用环境变量覆盖
                self._config = self._apply_env_overrides(self._config)
                self._config = self._resolve_env_vars_in_values(self._config)
            logger.info("配置已保存并刷新: %s", path)
            return True
        except Exception as exc:
            logger.error("保存配置失败: %s", exc)
            return False

    def _write_yaml(self) -> None:
        """将当前配置写回到 YAML 文件。"""
        if self.config_path and os.path.isfile(self.config_path):
            with open(self.config_path, 'w', encoding='utf-8') as f:
                yaml.dump(self._config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    # ------------------------------------------------------------------
    # Watchdog 热重载
    # ------------------------------------------------------------------

    def start_watching(self) -> None:
        """启动 Watchdog 文件监听，实现配置热重载。

        当配置文件发生变化时，自动调用 load() 重新加载。
        """
        if self._watchdog_active:
            logger.info("Watchdog 已在运行，跳过启动")
            return

        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler
        except ImportError:
            logger.warning(
                "watchdog 库未安装，跳过文件监听热重载。"
                "请执行: pip install watchdog"
            )
            return

        filepath = Path(self.config_path)
        watch_dir = filepath.parent
        watched_file = filepath.name

        if not watch_dir.exists():
            logger.warning("配置目录不存在，无法启动 Watchdog: %s", watch_dir)
            return

        class _ConfigChangeHandler(FileSystemEventHandler):
            """配置文件变化处理器。"""

            def __init__(self, manager: "ConfigManager", target_file: str) -> None:
                self.manager = manager
                self.target_file = target_file

            def on_modified(self, event) -> None:  # type: ignore[reportMissingType]
                if event.src_path.endswith(self.target_file):
                    logger.info(
                        "检测到配置文件变更: %s, 开始热重载",
                        event.src_path,
                    )
                    try:
                        self.manager.load()
                    except Exception as exc:  # noqa: BLE001
                        logger.error("配置热重载失败: %s", exc)

            def on_created(self, event) -> None:
                if event.src_path.endswith(self.target_file):
                    logger.info(
                        "配置文件创建: %s, 加载配置",
                        event.src_path,
                    )
                    try:
                        self.manager.load()
                    except Exception as exc:
                        logger.error("配置加载失败: %s", exc)

        handler = _ConfigChangeHandler(self, str(watched_file))
        self._watchdog = Observer()
        self._watch_handle = self._watchdog.schedule(
            handler, str(watch_dir), recursive=False
        )
        self._watchdog.start()
        self._watchdog_active = True

        logger.info(
            "Watchdog 已启动: 监听 %s (文件: %s)",
            watch_dir,
            watched_file,
        )

    def stop_watching(self) -> None:
        """停止 Watchdog 文件监听。"""
        if not self._watchdog_active or self._watchdog is None:
            return

        self._watchdog.stop()
        self._watchdog = None
        self._watchdog_active = False
        logger.info("Watchdog 已停止")

    # ------------------------------------------------------------------
    # 热重载回调
    # ------------------------------------------------------------------

    def on_reload(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """注册热重载回调函数。

        Args:
            callback: 配置变更时调用的函数，接收新配置字典为参数。
        """
        self._reload_callbacks.append(callback)

    def _notify_reload(self, old_config: Dict[str, Any], new_config: Dict[str, Any]) -> None:
        """通知所有注册的回调函数配置已变更。

        Args:
            old_config: 旧配置字典。
            new_config: 新配置字典。
        """
        for callback in self._reload_callbacks:
            try:
                callback(new_config)
            except Exception as exc:
                logger.error("热重载回调执行失败: %s", exc)
