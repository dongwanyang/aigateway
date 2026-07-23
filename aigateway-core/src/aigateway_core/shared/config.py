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
import dataclasses
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

from .integration_configs import (
    CLIPConfig,
    ComfyUIConfig,
    ConvCompressorConfig,
    PaddleOCRConfig,
    PromptCompressConfig,
    RAGRetrieverConfig,
    UnstructuredConfig,
)

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
        "workers": 1,
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
                        再缺省用 "./config.yaml"。若当前目录下不存在，
                        会向上搜索最多 3 层父目录。
        """
        # 确定配置文件路径
        resolved_path = config_path or os.environ.get(
            "AI_GATEWAY_CONFIG_PATH", ""
        )

        if not resolved_path:
            resolved_path = self._find_config_file()

        self.config_path = resolved_path

        self._config: Dict[str, Any] = {}
        self._lock = threading.RLock()
        self._reload_callbacks: list[Callable[[Dict[str, Any]], None]] = []
        self._watchdog: Any = None
        self._watch_handle: Any = None
        self._watchdog_active = False
        self._integration_configs: Optional["IntegrationConfigs"] = None

        # 加载配置
        self.load()

    @staticmethod
    def _find_config_file() -> str:
        """从当前目录向上搜索 config.yaml（最多 3 层）。

        搜索顺序：
        1. ./config.yaml
        2. ../config.yaml
        3. ../../config.yaml
        4. ../../../config.yaml

        Returns:
            找到的 config.yaml 绝对路径，或默认 "./config.yaml"。
        """
        current = Path.cwd()
        for _ in range(4):
            candidate = current / "config.yaml"
            if candidate.exists():
                return str(candidate)
            parent = current.parent
            if parent == current:
                break
            current = parent
        # 回退到默认值（_load_yaml 会 warning）
        return "./config.yaml"

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
        config = self._apply_environment_mode(config)

        # 验证配置（宽容模式 — 仅日志警告，不阻止加载）
        self._validate_config(config)

        with self._lock:
            old_config = copy.deepcopy(self._config)
            self._config = copy.deepcopy(config)

        # 解析集成配置（新增配置时自动包含在热重载路径中）
        self._integration_configs = parse_integration_configs(
            config, self._integration_configs
        )

        logger.info(
            "配置已加载: path=%s, keys=%s",
            self.config_path,
            list(config.keys()),
        )

        # 如果已有旧配置（非首次加载），通知热重载回调
        if old_config:
            self._notify_reload(old_config, config)

        return self._config

    def _apply_environment_mode(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """根据 AI_GATEWAY_ENV 环境变量应用环境模式覆盖。

        - production: 强制关闭 debug_mode，日志级别不低于 INFO
        - development: 启用 hot_reload 和 debug_mode

        Args:
            config: 配置字典。

        Returns:
            应用覆盖后的配置字典。
        """
        env = os.environ.get("AI_GATEWAY_ENV", "development")

        if env == "production":
            config["debug_mode"] = False
            obs = config.setdefault("observability", {})
            if isinstance(obs, dict) and obs.get("log_level", "info").lower() == "debug":
                obs["log_level"] = "info"
            logger.info("生产环境模式: debug_mode=False, log_level≥INFO")
        elif env == "development":
            config.setdefault("hot_reload", True)
            config.setdefault("debug_mode", True)
            # 安全警告：如果 config.yaml 显式写了 debug_mode=true 且未设置 AI_GATEWAY_ENV=production，
            # 提醒运维此配置不应被部署到共享/生产环境
            if config.get("debug_mode") is True:
                logger.warning(
                    "[SECURITY] debug_mode=True 在开发模式下生效。"
                    "若部署到非本地环境，请设置 AI_GATEWAY_ENV=production 或将 debug_mode 改为 False。"
                )
            logger.info("开发环境模式: hot_reload=True, debug_mode=True")
        else:
            logger.info("运行环境: %s", env)

        return config

    def _validate_config(self, config: Dict[str, Any]) -> None:
        """验证配置结构和安全性（宽容模式）。

        仅记录 WARNING/ERROR 日志，不阻止配置加载。
        """
        allowed_top_level = {
            "server", "auth", "plugins", "providers", "embedding",
            "observability", "hot_reload", "debug_mode", "debug", "infrastructure",
            "cache", "media_optimization", "circuit_breaker", "rate_limiter",
            "streaming", "generation_optimization", "code_rag",
        }

        # 环境变量覆盖产生的扁平键（AI_GATEWAY_* 去前缀后的小写形式）
        # 这些不算 "未识别" — 它们来自合法的环境变量配置
        env_prefix = "AI_GATEWAY_"
        env_generated_keys = {
            k[len(env_prefix):].lower()
            for k in os.environ
            if k.startswith(env_prefix)
        }

        # 检查未识别的顶层字段（排除环境变量覆盖产生的键）
        unknown_fields = set(config.keys()) - allowed_top_level - env_generated_keys
        if unknown_fields:
            logger.warning("config.yaml 包含未识别的顶层字段: %s", list(unknown_fields))

        # 检查 server.port 范围
        server_cfg = config.get("server", {})
        if isinstance(server_cfg, dict):
            port = server_cfg.get("port")
            if port is not None and not (1024 <= int(port) <= 65535):
                logger.error("config.yaml server.port 值无效 (%s)，应为 1024-65535", port)

        # 检查 provider api_key 明文警告
        providers = config.get("providers", {})
        if isinstance(providers, dict):
            for provider_name, provider_cfg in providers.items():
                if isinstance(provider_cfg, dict):
                    api_key = provider_cfg.get("api_key", "")
                    if isinstance(api_key, str) and api_key.startswith("sk-") and len(api_key) > 10:
                        # 检查不是 ${ENV_VAR} 语法
                        if not api_key.startswith("${"):
                            logger.warning(
                                "providers.%s.api_key 疑似明文密钥，建议使用 ${ENV_VAR} 语法引用环境变量",
                                provider_name,
                            )

        # 检查 plugins depends_on 引用
        plugins = config.get("plugins", [])
        if isinstance(plugins, list):
            plugin_names = {p.get("name") for p in plugins if isinstance(p, dict)}
            for plugin in plugins:
                if isinstance(plugin, dict):
                    deps = plugin.get("depends_on", [])
                    for dep in deps:
                        if dep not in plugin_names:
                            logger.warning(
                                "插件 %s 的 depends_on 引用了不存在的插件: %s",
                                plugin.get("name", "unknown"),
                                dep,
                            )

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
                # 共享锁:与 admin 端 _flocked_inplace_write 的排它锁互斥,
                # 避免 Watchdog 在 admin 原地写中途读到半截 YAML。
                try:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                    try:
                        data = yaml.safe_load(f)
                    finally:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except ImportError:
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

        Note:
            Explicit ``null`` values and missing keys are treated the same
            (both return ``default``).  To distinguish them, use ``get_raw()``.
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

    def get_raw(self, path: str, default: Any = None) -> Tuple[Any, bool]:
        """读取配置值，区分显式 null 和缺失键。

        Returns:
            (value, found) — found=True 表示键存在（可能为 null），
            found=False 表示键不存在。
        """
        keys = path.split(".")
        current: Any = self._config
        with self._lock:
            for key in keys:
                if isinstance(current, dict):
                    if key not in current:
                        return (default, False)
                    current = current[key]
                else:
                    return (default, False)
        return (current, True)

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

    @property
    def integration_configs(self) -> "IntegrationConfigs":
        """获取当前集成配置实例。

        Returns:
            IntegrationConfigs 实例，包含所有 7 个集成配置 dataclass。
        """
        if self._integration_configs is None:
            self._integration_configs = parse_integration_configs(self._config)
        return self._integration_configs

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
            logger.info("配置已保存: %s", path)
            return True
        except Exception as exc:
            logger.error("保存配置失败: %s", exc)
            return False

    def _write_yaml(self) -> None:
        """将当前配置写回到 YAML 文件。

        只写入合法的配置节，过滤掉由环境变量覆盖产生的扁平键，
        避免污染 YAML 文件结构。

        WARNING: 新增的配置段必须加入 writable_keys 集合，否则会被静默丢弃。
        添加新段时同步更新 config.yaml.template 和此处的白名单。
        """
        if self.config_path and os.path.isfile(self.config_path):
            # 合法配置节：这些键应该持久化到 YAML
            writable_keys = {
                "server", "auth", "plugins", "providers",
                "embedding", "observability", "infrastructure",
                "hot_reload", "debug_mode", "debug", "cache",
            }
            clean_config = {k: v for k, v in self._config.items() if k in writable_keys}
            with open(self.config_path, 'w', encoding='utf-8') as f:
                yaml.dump(clean_config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

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

    # ------------------------------------------------------------------
    # 安全热重载
    # ------------------------------------------------------------------

    async def safe_reload(self, key_store: Any = None) -> bool:
        """安全热重载：先验证新配置再交换，确保不中断服务。

        流程:
        1. 从 YAML 重新加载配置
        2. 应用环境变量覆盖
        3. 验证新配置
        4. 验证通过 → 原子交换
        5. 验证失败 → 保持旧配置，记录 ERROR

        Args:
            key_store: 可选的 KeyStore 实例，用于广播配置变更。

        Returns:
            是否重载成功。
        """
        import time as _time

        try:
            new_config = self._load_yaml(self.config_path)
            new_config = self._apply_env_overrides(new_config)
            new_config = self._resolve_env_vars_in_values(new_config)
        except Exception as exc:
            logger.error("配置重载失败: 文件加载异常: %s", exc)
            self._inc_reload_failure_metric()
            return False

        # 验证新配置
        issues = self._validate_config_strict(new_config)
        has_errors = any(i.get("level") == "ERROR" for i in issues)

        if has_errors:
            logger.error("配置重载失败: 验证不通过: %s", issues)
            self._inc_reload_failure_metric()
            return False

        # 原子交换
        self.atomic_swap(new_config)
        self._inc_reload_success_metric()
        logger.info("配置安全重载完成")

        # 广播到其他实例
        if key_store and hasattr(key_store, "broadcast_config_reload"):
            try:
                await key_store.broadcast_config_reload(
                    config_version=str(_time.time())
                )
            except Exception as exc:
                logger.warning("配置变更广播失败: %s", exc)

        return True

    def _validate_config_strict(self, config: Dict[str, Any]) -> list:
        """严格验证配置，返回问题列表。

        Returns:
            问题列表 [{"level": "ERROR"|"WARNING", "message": "..."}]
        """
        issues = []
        allowed_top_level = {
            "server", "auth", "plugins", "providers", "embedding",
            "observability", "hot_reload", "debug_mode", "debug", "infrastructure",
            "cache", "media_optimization", "circuit_breaker", "rate_limiter",
            "streaming", "generation_optimization", "code_rag",
        }

        # 检查未识别的顶层字段
        unknown_fields = set(config.keys()) - allowed_top_level
        if unknown_fields:
            issues.append({"level": "WARNING", "message": f"未识别的顶层字段: {list(unknown_fields)}"})

        # 检查 server.port 范围
        server_cfg = config.get("server", {})
        if isinstance(server_cfg, dict):
            port = server_cfg.get("port")
            if port is not None:
                try:
                    port_int = int(port)
                    if not (1024 <= port_int <= 65535):
                        issues.append({"level": "ERROR", "message": f"server.port 必须在 1024-65535 之间，当前值: {port}"})
                except (TypeError, ValueError):
                    issues.append({"level": "ERROR", "message": f"server.port 必须为整数，当前值: {port}"})

        # 检查 provider api_key
        providers = config.get("providers", {})
        if isinstance(providers, dict):
            for provider_name, provider_cfg in providers.items():
                if isinstance(provider_cfg, dict):
                    api_key = provider_cfg.get("api_key", "")
                    if isinstance(api_key, str) and api_key.startswith("sk-") and len(api_key) > 10:
                        if not api_key.startswith("${"):
                            issues.append({
                                "level": "WARNING",
                                "message": f"providers.{provider_name}.api_key 疑似明文密钥",
                            })

        return issues

    def _inc_reload_success_metric(self) -> None:
        """增加配置重载成功计数器。"""
        try:
            from .metrics import get_metrics_collector
            metrics = get_metrics_collector()
            metrics.record_request("INTERNAL", "/config-reload", "200")
        except Exception:
            pass

    def _inc_reload_failure_metric(self) -> None:
        """增加配置重载失败计数器。"""
        try:
            from .metrics import get_metrics_collector
            metrics = get_metrics_collector()
            metrics.record_request("INTERNAL", "/config-reload", "500")
        except Exception:
            pass


# ------------------------------------------------------------------
# 集成配置解析
# ------------------------------------------------------------------

# 环境变量前缀到 dataclass 字段名的映射
# 格式: AI_GATEWAY_<CONFIG_NAME>_<FIELD_NAME>
_INTEGRATION_ENV_PREFIXES = {
    "PROMPT_COMPRESS": PromptCompressConfig,
    "CLIP": CLIPConfig,
    "COMFYUI": ComfyUIConfig,
    "RAG_RETRIEVER": RAGRetrieverConfig,
    "CONV_COMPRESSOR": ConvCompressorConfig,
    "PADDLEOCR": PaddleOCRConfig,
    "UNSTRUCTURED": UnstructuredConfig,
}

# 字段值范围约束 — {(ConfigClass, field_name): (min, max)} 或 callable validator
_FIELD_VALIDATORS: Dict[Tuple[type, str], Any] = {
    (PromptCompressConfig, "compression_ratio"): {"min": 0.0, "max": 1.0},
    (CLIPConfig, "batch_size"): {"min": 1},
    (ComfyUIConfig, "connect_timeout"): {"min": 1},
    (ComfyUIConfig, "execution_timeout"): {"min": 1},
    (ComfyUIConfig, "ws_reconnect_attempts"): {"min": 0},
    (RAGRetrieverConfig, "top_k"): {"min": 1},
    (RAGRetrieverConfig, "similarity_threshold"): {"min": 0.0, "max": 1.0},
    (RAGRetrieverConfig, "chunk_size"): {"min": 1},
    (RAGRetrieverConfig, "chunk_overlap"): {"min": 0},
    (ConvCompressorConfig, "max_history"): {"min": 1},
    (ConvCompressorConfig, "max_token_limit"): {"min": 1},
    (ConvCompressorConfig, "summary_interval"): {"min": 1},
    (UnstructuredConfig, "strategy"): {"choices": ["auto", "fast", "hi_res"]},
}


def _extract_plugin_config(config_dict: Dict[str, Any], plugin_name: str) -> Dict[str, Any]:
    """从 plugins 列表中提取指定插件的 config 字典。

    Args:
        config_dict: 完整配置字典。
        plugin_name: 插件名称。

    Returns:
        插件的 config 字典，若不存在则返回空字典。
    """
    plugins = config_dict.get("plugins", [])
    if not isinstance(plugins, list):
        return {}
    for plugin in plugins:
        if isinstance(plugin, dict) and plugin.get("name") == plugin_name:
            cfg = plugin.get("config", {})
            return cfg if isinstance(cfg, dict) else {}
    return {}


def _get_nested(config_dict: Dict[str, Any], dotted_path: str) -> Dict[str, Any]:
    """按点分隔路径获取嵌套字典。

    Args:
        config_dict: 完整配置字典。
        dotted_path: 点分隔路径，如 "generation_optimization.token_compressor.clip"

    Returns:
        找到的字典，若不存在则返回空字典。
    """
    keys = dotted_path.split(".")
    current: Any = config_dict
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return {}
        if current is None:
            return {}
    return current if isinstance(current, dict) else {}


def _apply_env_overrides_for_config(
    config_name: str, raw_values: Dict[str, Any]
) -> Dict[str, Any]:
    """为特定集成配置应用环境变量覆盖。

    查找形如 AI_GATEWAY_<CONFIG_NAME>_<FIELD_NAME> 的环境变量。

    Args:
        config_name: 配置名称（大写），如 "PROMPT_COMPRESS"。
        raw_values: 已从 YAML 提取的配置值字典。

    Returns:
        应用环境变量覆盖后的值字典。
    """
    prefix = f"AI_GATEWAY_{config_name}_"
    result = dict(raw_values)

    for env_key, env_value in os.environ.items():
        if env_key.startswith(prefix):
            field_name = env_key[len(prefix):].lower()
            parsed = ConfigManager._parse_env_value(env_value)
            result[field_name] = parsed

    return result


def _validate_and_build(
    config_class: type,
    values: Dict[str, Any],
    previous: Optional[Any] = None,
) -> Any:
    """验证配置值并构建 dataclass 实例。

    对每个字段进行类型验证和范围检查。
    如果某个字段无效，则使用 previous 实例中的对应值（若有），
    否则使用 dataclass 默认值。

    Args:
        config_class: dataclass 类型。
        values: 配置值字典。
        previous: 之前有效的配置实例（用于回退）。

    Returns:
        已验证的 dataclass 实例。
    """
    fields = dataclasses.fields(config_class)
    valid_kwargs: Dict[str, Any] = {}

    for f in fields:
        if f.name not in values:
            # 没有提供值，使用 previous 或 default
            if previous is not None and hasattr(previous, f.name):
                valid_kwargs[f.name] = getattr(previous, f.name)
            # 否则让 dataclass 使用自己的默认值（不传此字段）
            continue

        raw_value = values[f.name]

        # 类型验证
        expected_type = f.type
        if not _check_type(raw_value, expected_type):
            logger.warning(
                "集成配置 %s.%s 类型无效: 期望 %s，实际 %r，保留旧值",
                config_class.__name__, f.name, expected_type, raw_value,
            )
            if previous is not None and hasattr(previous, f.name):
                valid_kwargs[f.name] = getattr(previous, f.name)
            continue

        # 范围/约束验证
        constraint = _FIELD_VALIDATORS.get((config_class, f.name))
        if constraint and not _check_constraint(raw_value, constraint):
            logger.warning(
                "集成配置 %s.%s 值越界: %r，约束 %s，保留旧值",
                config_class.__name__, f.name, raw_value, constraint,
            )
            if previous is not None and hasattr(previous, f.name):
                valid_kwargs[f.name] = getattr(previous, f.name)
            continue

        valid_kwargs[f.name] = raw_value

    return config_class(**valid_kwargs)


def _check_type(value: Any, type_hint: str) -> bool:
    """简单类型检查，基于 dataclass field type 注解字符串。

    Args:
        value: 待检查值。
        type_hint: 类型注解字符串（如 "bool", "float", "str", "int"）。

    Returns:
        是否类型匹配。
    """
    # 处理 Optional 类型
    if "Optional" in str(type_hint):
        if value is None:
            return True
        # 提取内部类型
        inner = str(type_hint).replace("Optional[", "").rstrip("]")
        return _check_type(value, inner)

    # 处理 List 类型
    if "List" in str(type_hint) or "list" in str(type_hint):
        return isinstance(value, list)

    # 基础类型检查
    type_str = str(type_hint).lower()
    if "bool" in type_str:
        return isinstance(value, bool)
    if "int" in type_str:
        # int 类型不应接受 bool（Python 中 bool 是 int 子类）
        return isinstance(value, int) and not isinstance(value, bool)
    if "float" in type_str:
        # float 也接受 int（自动转换）
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if "str" in type_str:
        return isinstance(value, str)

    return True  # 未知类型，通过


def _check_constraint(value: Any, constraint: Dict[str, Any]) -> bool:
    """检查值是否满足约束条件。

    Args:
        value: 待检查值。
        constraint: 约束字典，可包含 "min", "max", "choices"。

    Returns:
        是否满足约束。
    """
    if "choices" in constraint:
        return value in constraint["choices"]

    if "min" in constraint and value < constraint["min"]:
        return False
    if "max" in constraint and value > constraint["max"]:
        return False

    return True


class IntegrationConfigs:
    """持有所有 7 个集成配置 dataclass 实例的容器。"""

    def __init__(
        self,
        prompt_compress: PromptCompressConfig,
        clip: CLIPConfig,
        comfyui: ComfyUIConfig,
        rag_retriever: RAGRetrieverConfig,
        conv_compressor: ConvCompressorConfig,
        paddleocr: PaddleOCRConfig,
        unstructured: UnstructuredConfig,
    ) -> None:
        self.prompt_compress = prompt_compress
        self.clip = clip
        self.comfyui = comfyui
        self.rag_retriever = rag_retriever
        self.conv_compressor = conv_compressor
        self.paddleocr = paddleocr
        self.unstructured = unstructured


def parse_integration_configs(
    config_dict: Dict[str, Any],
    previous: Optional[IntegrationConfigs] = None,
) -> IntegrationConfigs:
    """从完整配置字典解析所有 7 个集成配置。

    解析流程：
    1. 从 config_dict 中提取各配置节的原始值
    2. 应用环境变量覆盖（AI_GATEWAY_<NAME>_<FIELD>）
    3. 验证类型和范围
    4. 构建 dataclass 实例（无效值回退到 previous 或默认值）

    YAML 配置路径映射:
    - plugins.[name="prompt_compress"].config → PromptCompressConfig
    - generation_optimization.token_compressor.clip → CLIPConfig
    - generation_optimization.draft_workflow.comfyui → ComfyUIConfig
    - plugins.[name="rag_retriever"].config → RAGRetrieverConfig
    - plugins.[name="conv_compressor"].config → ConvCompressorConfig
    - media_optimization.image.paddleocr → PaddleOCRConfig
    - media_optimization.document.unstructured → UnstructuredConfig

    Args:
        config_dict: 完整 YAML 配置字典。
        previous: 之前有效的配置实例（用于无效值回退）。

    Returns:
        IntegrationConfigs 实例，包含所有 7 个配置。
    """
    # 1. 提取各配置节的原始值
    prompt_compress_raw = _extract_plugin_config(config_dict, "prompt_compress")
    clip_raw = _get_nested(config_dict, "generation_optimization.token_compressor.clip")
    comfyui_raw = _get_nested(config_dict, "generation_optimization.draft_workflow.comfyui")
    rag_retriever_raw = _extract_plugin_config(config_dict, "rag_retriever")
    conv_compressor_raw = _extract_plugin_config(config_dict, "conv_compressor")
    paddleocr_raw = _get_nested(config_dict, "media_optimization.image.paddleocr")
    unstructured_raw = _get_nested(config_dict, "media_optimization.document.unstructured")

    # 2. 应用环境变量覆盖
    prompt_compress_raw = _apply_env_overrides_for_config("PROMPT_COMPRESS", prompt_compress_raw)
    clip_raw = _apply_env_overrides_for_config("CLIP", clip_raw)
    comfyui_raw = _apply_env_overrides_for_config("COMFYUI", comfyui_raw)
    rag_retriever_raw = _apply_env_overrides_for_config("RAG_RETRIEVER", rag_retriever_raw)
    conv_compressor_raw = _apply_env_overrides_for_config("CONV_COMPRESSOR", conv_compressor_raw)
    paddleocr_raw = _apply_env_overrides_for_config("PADDLEOCR", paddleocr_raw)
    unstructured_raw = _apply_env_overrides_for_config("UNSTRUCTURED", unstructured_raw)

    # 3 & 4. 验证并构建 dataclass 实例
    prompt_compress = _validate_and_build(
        PromptCompressConfig,
        prompt_compress_raw,
        previous.prompt_compress if previous else None,
    )
    clip = _validate_and_build(
        CLIPConfig,
        clip_raw,
        previous.clip if previous else None,
    )
    comfyui = _validate_and_build(
        ComfyUIConfig,
        comfyui_raw,
        previous.comfyui if previous else None,
    )
    rag_retriever = _validate_and_build(
        RAGRetrieverConfig,
        rag_retriever_raw,
        previous.rag_retriever if previous else None,
    )
    conv_compressor = _validate_and_build(
        ConvCompressorConfig,
        conv_compressor_raw,
        previous.conv_compressor if previous else None,
    )
    paddleocr = _validate_and_build(
        PaddleOCRConfig,
        paddleocr_raw,
        previous.paddleocr if previous else None,
    )
    unstructured = _validate_and_build(
        UnstructuredConfig,
        unstructured_raw,
        previous.unstructured if previous else None,
    )

    return IntegrationConfigs(
        prompt_compress=prompt_compress,
        clip=clip,
        comfyui=comfyui,
        rag_retriever=rag_retriever,
        conv_compressor=conv_compressor,
        paddleocr=paddleocr,
        unstructured=unstructured,
    )
