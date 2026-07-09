"""
PipelineEngine — 异步插件管线引擎
================================

按配置顺序执行插件管线，支持短路（should_stop=True 时跳过后续插件）、
依赖校验和插件级耗时追踪。

根据 API_CONTRACT.md _meta.plugin_trace 定义。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Protocol

from .context import PipelineContext
from aigateway_core.shared.plugin_registry import PluginRegistry
from aigateway_core.shared.trace_event import TraceCollector, TraceEvent

logger = logging.getLogger(__name__)


def _truncate(s: str, n: int = 500) -> str:
    """截断字符串用于 debug payload(避免 Redis hash 写入过大)."""
    return s if len(s) <= n else s[:n] + "..."


class Plugin(Protocol):
    """插件接口协议，所有管线插件必须实现此接口。"""

    name: str
    enabled: bool
    depends_on: List[str]
    pipeline_kind: str

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        ...


class PipelineEngine:
    """异步插件管线引擎。"""

    def __init__(self, registry: PluginRegistry, pipeline_kind: str = "understanding") -> None:
        self.registry = registry
        self.pipeline_kind = pipeline_kind
        self._ordered_plugins: List[Plugin] = []
        self._initialized = False

    def initialize(self) -> None:
        all_plugins = self.registry.get_all(pipeline_kind=self.pipeline_kind)
        enabled_plugins = [plugin for plugin in all_plugins if getattr(plugin, "enabled", True)]
        self._ordered_plugins = self._topological_sort(enabled_plugins)
        self._initialized = True

        logger.info(
            "PipelineEngine[%s] 已初始化: %d 个插件按序排列",
            self.pipeline_kind,
            len(self._ordered_plugins),
        )
        for index, plugin in enumerate(self._ordered_plugins):
            deps = getattr(plugin, "depends_on", [])
            logger.debug("  [%d] %s (依赖: %s)", index, plugin.name, deps)

    async def execute(self, request: Dict[str, Any]) -> Dict[str, Any]:
        if not self._initialized:
            self.initialize()

        ctx = PipelineContext(request=request, trace_id=request.get("trace_id", ""), pipeline_kind=self.pipeline_kind)
        ctx.should_stream = bool(request.get("stream", False))
        ctx = await self.execute_ctx(ctx)
        return self._build_response(ctx)

    async def execute_ctx(self, ctx: PipelineContext) -> PipelineContext:
        if not self._initialized:
            self.initialize()

        pipeline_start = time.monotonic()

        try:
            for plugin in self._ordered_plugins:
                if ctx.should_stop:
                    skipped_ms = (time.monotonic() - pipeline_start) * 1000
                    collector = TraceCollector.current()
                    if collector:
                        collector.emit(TraceEvent(
                            trace_id=ctx.trace_id,
                            ts=time.monotonic(),
                            stage=plugin.name,
                            kind="plugin",
                            name=f"{plugin.name}.skip",
                            duration_ms=round(skipped_ms, 2),
                            status="skip",
                        ))
                    logger.debug(
                        "插件 %s 被跳过 (should_stop=True, request_id=%s)",
                        plugin.name,
                        ctx.request_id,
                    )
                    continue

                plugin_name = plugin.name
                plugin_start = time.monotonic()

                try:
                    ctx = await plugin.execute(ctx)
                except Exception as exc:
                    elapsed_ms = (time.monotonic() - plugin_start) * 1000
                    collector = TraceCollector.current()
                    if collector:
                        collector.emit(TraceEvent(
                            trace_id=ctx.trace_id,
                            ts=time.monotonic(),
                            stage=plugin_name,
                            kind="plugin",
                            name=f"{plugin_name}.execute",
                            duration_ms=round(elapsed_ms, 2),
                            status="error",
                        ))
                    logger.error(
                        "插件 %s 执行失败: %s, request_id=%s",
                        plugin_name,
                        exc,
                        ctx.request_id,
                    )
                    continue

                elapsed_ms = (time.monotonic() - plugin_start) * 1000
                collector = TraceCollector.current()
                if collector:
                    collector.emit(TraceEvent(
                        trace_id=ctx.trace_id,
                        ts=time.monotonic(),
                        stage=plugin_name,
                        kind="plugin",
                        name=f"{plugin_name}.execute",
                        duration_ms=round(elapsed_ms, 2),
                        status="ok",
                    ))
                    collector.emit_debug(
                        stage=plugin_name,
                        name=f"{plugin_name}.execute",
                        duration_ms=elapsed_ms,
                        status="ok",
                        dimension="plugin",
                        payload={"input_summary": _truncate(str(ctx.request.get("messages", ""))[:500])},
                    )
                logger.debug(
                    "插件 %s 执行完毕: %.2fms, request_id=%s",
                    plugin_name,
                    elapsed_ms,
                    ctx.request_id,
                )

            total_ms = (time.monotonic() - pipeline_start) * 1000
            logger.info(
                "管线[%s]执行完成: request_id=%s, total=%.2fms, stopped=%s",
                self.pipeline_kind,
                ctx.request_id,
                total_ms,
                ctx.should_stop,
            )
            return ctx

        except Exception as exc:
            logger.error(
                "管线[%s]执行发生未捕获异常: %s, request_id=%s",
                self.pipeline_kind,
                exc,
                getattr(ctx, "request_id", "unknown"),
            )
            ctx.should_stop = True
            ctx.extra.setdefault("pipeline_error", str(exc))
            return ctx

    def _topological_sort(self, plugins: List[Plugin]) -> List[Plugin]:
        name_to_plugin: Dict[str, Plugin] = {plugin.name: plugin for plugin in plugins}
        in_degree: Dict[str, int] = {plugin.name: 0 for plugin in plugins}
        dependents: Dict[str, List[str]] = {plugin.name: [] for plugin in plugins}

        for plugin in plugins:
            deps = getattr(plugin, "depends_on", [])
            for dep in deps:
                if dep in name_to_plugin:
                    in_degree[plugin.name] += 1
                    dependents[dep].append(plugin.name)
                else:
                    logger.warning(
                        "插件 %s 依赖 %s 不存在或被禁用，已忽略",
                        plugin.name,
                        dep,
                    )

        queue: List[str] = []
        for name, degree in in_degree.items():
            if degree == 0:
                queue.append(name)

        ordered_names: List[str] = []
        while queue:
            node = queue.pop(0)
            ordered_names.append(node)

            for dependent in dependents[node]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        if len(ordered_names) != len(plugins):
            missing = [plugin.name for plugin in plugins if plugin.name not in ordered_names]
            logger.error("插件依赖存在循环: %s", missing)
            return plugins

        return [name_to_plugin[name] for name in ordered_names]

    def _build_response(self, ctx: PipelineContext) -> Dict[str, Any]:
        response_data: Dict[str, Any] = {}

        if ctx.response:
            import json
            try:
                parsed = json.loads(ctx.response)
                response_data = parsed.get("data", parsed)
            except (json.JSONDecodeError, AttributeError):
                response_data = {"raw": ctx.response}
        else:
            response_data = {"status": "needs_completion"}

        return {
            "data": response_data,
            "message": "success",
            "_meta": {
                "cache_hit": bool(ctx.response),
                "cache_tier": "L1" if ctx.response else None,
                "plugin_trace": ctx.get_plugin_trace(),
                "routed_to": None,
            },
        }

    def _build_error_response(self, message: str) -> Dict[str, Any]:
        return {
            "error": {
                "code": "internal_error",
                "message": f"Internal gateway error: {message}",
            }
        }


__all__ = ["PipelineEngine", "Plugin"]
