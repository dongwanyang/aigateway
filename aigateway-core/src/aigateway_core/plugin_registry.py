"""
PluginRegistry — 插件注册表
==========================

管理插件的生命周期：注册、排序、依赖校验。
启动时校验 depends_on，跳过未启用或顺序错误的插件。

根据 TECH_SPEC.md 插件管线配置定义。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Type

from .context import PipelineContext

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# 插件注册结构
# ------------------------------------------------------------------


class PluginRegistration:
    """单个插件的注册信息。

    属性:
        name: 插件名称，如 "prompt_compress"。
        plugin_class: 插件类（需要可调用返回 Plugin 实例）。
        enabled: 是否启用，默认 True。
        depends_on: 依赖的其他插件名称列表。
        priority: 执行优先级，数字越小越先执行。
        config: 插件配置参数。
    """

    def __init__(
        self,
        name: str,
        plugin_class: Type[Any],
        enabled: bool = True,
        depends_on: Optional[List[str]] = None,
        priority: int = 0,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.name = name
        self.plugin_class = plugin_class
        self.enabled = enabled
        self.depends_on = depends_on or []
        self.priority = priority
        self.config = config or {}


# ------------------------------------------------------------------
# 注册表
# ------------------------------------------------------------------


class PluginRegistry:
    """插件注册中心。

    管理所有插件的注册、排序和依赖校验。
    插件可按优先级排序执行，依赖关系会被自动解析。

    属性:
        _registrations: 已注册的插件信息字典，key 为插件名。
    """

    def __init__(self) -> None:
        self._registrations: Dict[str, PluginRegistration] = {}

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        plugin_class: Type[Any],
        enabled: bool = True,
        depends_on: Optional[List[str]] = None,
        priority: int = 0,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        """注册一个插件。

        Args:
            name: 插件名称，必须全局唯一。
            plugin_class: 插件类（需要有 execute(ctx: PipelineContext) 方法）。
            enabled: 是否启用，默认 True。
            depends_on: 依赖的插件名称列表，默认 []。
            priority: 执行优先级，数字越小越先执行，默认 0。
            config: 插件配置参数字典。

        Raises:
            ValueError: 插件名重复时抛出。
        """
        if name in self._registrations:
            raise ValueError(f"插件 '{name}' 已注册，不能重复注册")

        self._registrations[name] = PluginRegistration(
            name=name,
            plugin_class=plugin_class,
            enabled=enabled,
            depends_on=depends_on or [],
            priority=priority,
            config=config,
        )

        logger.info(
            "插件注册: name=%s, enabled=%s, depends_on=%s, priority=%d",
            name,
            enabled,
            depends_on or [],
            priority,
        )

    def unregister(self, name: str) -> None:
        """注销一个插件。

        Args:
            name: 插件名称。

        Raises:
            KeyError: 插件不存在时抛出。
        """
        if name not in self._registrations:
            raise KeyError(f"插件 '{name}' 未注册")

        del self._registrations[name]
        logger.info("插件已注销: name=%s", name)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get(self, name: str) -> Optional[PluginRegistration]:
        """查询插件注册信息。

        Args:
            name: 插件名称。

        Returns:
            插件注册信息，不存在则返回 None。
        """
        return self._registrations.get(name)

    def get_all(self) -> List[Any]:
        """获取所有已注册插件的实例列表。

        按 priority 升序排列（数字小的先执行）。
        返回的是实例化后的插件对象（调用 plugin_class()）。

        Returns:
            插件实例列表。
        """
        registrations = sorted(
            self._registrations.values(),
            key=lambda r: r.priority,
        )

        instances: List[Any] = []
        for reg in registrations:
            try:
                # 使用 config 初始化插件实例
                instance = reg.plugin_class(**reg.config)
                instance.name = reg.name  # type: ignore[attr-defined]
                instance.enabled = reg.enabled  # type: ignore[attr-defined]
                instance.depends_on = reg.depends_on  # type: ignore[attr-defined]
                instances.append(instance)
            except TypeError as exc:
                logger.warning(
                    "插件 '%s' 实例化失败（配置参数不匹配）: %s",
                    reg.name,
                    exc,
                )

        return instances

    def get_enabled_names(self) -> List[str]:
        """获取所有已启用插件的名称列表。

        Returns:
            插件名称列表。
        """
        return [name for name, reg in self._registrations.items() if reg.enabled]

    # ------------------------------------------------------------------
    # 依赖校验
    # ------------------------------------------------------------------

    def validate_dependencies(self) -> List[str]:
        """校验所有插件的依赖关系。

        检查：
        1. 所有 depends_on 引用的插件是否存在
        2. 是否存在循环依赖

        Returns:
            校验失败的错误消息列表，为空表示全部通过。
        """
        errors: List[str] = []
        names = set(self._registrations.keys())

        # 检查缺失依赖
        for name, reg in self._registrations.items():
            for dep in reg.depends_on:
                if dep not in names:
                    errors.append(
                        f"插件 '{name}' 依赖 '{dep}' 但未注册"
                    )

        # 检查循环依赖（DFS）
        visited: set[str] = set()
        rec_stack: set[str] = set()
        cycle_nodes: List[str] = []

        def dfs(node: str) -> bool:
            visited.add(node)
            rec_stack.add(node)

            reg = self._registrations.get(node)
            if reg:
                for dep in reg.depends_on:
                    if dep not in visited:
                        if dfs(dep):
                            return True
                    elif dep in rec_stack:
                        cycle_nodes.append(dep)
                        return True

            rec_stack.discard(node)
            return False

        for name in self._registrations:
            if name not in visited:
                if dfs(name):
                    errors.append(
                        f"插件依赖存在循环: {cycle_nodes}"
                    )
                    break

        if errors:
            logger.warning("插件依赖校验失败: %s", errors)
        else:
            logger.info("插件依赖校验通过: %d 个插件", len(self._registrations))

        return errors

    # ------------------------------------------------------------------
    # 批量注册（从配置）
    # ------------------------------------------------------------------

    def register_from_config(self, configs: List[Dict[str, Any]]) -> None:
        """从配置字典列表批量注册插件。

        Args:
            configs: 每个元素包含:
                - name (str): 插件名称
                - enabled (bool): 是否启用
                - depends_on (List[str]): 依赖列表
                - priority (int): 优先级
                - plugin_class (Type[Any]): 插件类
                - config (Dict[str, Any]): 插件配置
        """
        for cfg in configs:
            name = cfg.get("name")
            plugin_class = cfg.get("plugin_class")

            if not name or not plugin_class:
                logger.warning("配置缺少 name 或 plugin_class，跳过: %s", cfg)
                continue

            self.register(
                name=name,
                plugin_class=plugin_class,
                enabled=cfg.get("enabled", True),
                depends_on=cfg.get("depends_on", []),
                priority=cfg.get("priority", 0),
                config=cfg.get("config", {}),
            )

    # ------------------------------------------------------------------
    # 统计信息
    # ------------------------------------------------------------------

    def summary(self) -> Dict[str, Any]:
        """获取注册表的统计摘要。

        Returns:
            包含插件计数、启用状态等信息的字典。
        """
        total = len(self._registrations)
        enabled = sum(1 for r in self._registrations.values() if r.enabled)
        disabled = total - enabled

        return {
            "total": total,
            "enabled": enabled,
            "disabled": disabled,
            "plugins": {
                name: {
                    "enabled": reg.enabled,
                    "depends_on": reg.depends_on,
                    "priority": reg.priority,
                }
                for name, reg in self._registrations.items()
            },
        }
