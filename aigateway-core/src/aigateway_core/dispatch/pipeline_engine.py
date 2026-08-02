"""
PipelineEngine — 异步插件管线引擎
================================

按配置顺序执行插件管线，支持短路（should_stop=True 时跳过后续插件）、
依赖校验和插件级耗时追踪。

根据 API_CONTRACT.md _meta.plugin_trace 定义。
"""

from __future__ import annotations

import asyncio
import copy
import logging
import re
import time
from typing import Any, Protocol

from aigateway_core.shared.plugin_registry import PluginRegistry
from aigateway_core.shared.trace_event import TraceCollector, TraceEvent

from .context import PipelineContext, RequestContext

logger = logging.getLogger(__name__)


def _truncate(s: str, n: int = 500) -> str:
    """截断字符串用于 debug payload(避免 Redis hash 写入过大)."""
    return s if len(s) <= n else s[:n] + "..."


def _sanitize_exc(exc: BaseException, max_len: int = 200) -> str:
    """脱敏异常字符串中的 URL 凭据后截断。"""
    s = str(exc)
    # 替换 URL 中的 user:pass@host 为 ***@***
    s = re.sub(r'(?<=://)[^:@]+(?=@)', '***', s)
    return s[:max_len]


class Plugin(Protocol):
    """插件接口协议，所有管线插件必须实现此接口。"""

    name: str
    enabled: bool
    depends_on: list[str]
    pipeline_kind: str

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        ...


_ORPHANED_PLUGIN_TASKS: set[asyncio.Task[PipelineContext]] = set()


def _safe_context_copy(value: Any) -> Any:
    try:
        return copy.deepcopy(value)
    except Exception:
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, list):
            return list(value)
        return value


def _clone_pipeline_context(ctx: PipelineContext) -> PipelineContext:
    """Isolate a timed-out plugin from the response context returned to callers."""
    cloned = PipelineContext(
        request=_safe_context_copy(ctx.request),
        trace_id=ctx.trace_id,
        response=ctx.response,
        should_stop=ctx.should_stop,
        should_stream=ctx.should_stream,
        request_id=ctx.request_id,
        user_id=ctx.user_id,
        extra=_safe_context_copy(ctx.extra),
        pipeline_kind=ctx.pipeline_kind,
        is_multimodal=ctx.is_multimodal,
        total_token_savings=ctx.total_token_savings,
        request_context=ctx.request_context,
    )
    if hasattr(ctx, "_skip_names"):
        cloned._skip_names = set(ctx._skip_names)
    return cloned


def _merge_pipeline_context(
    target: PipelineContext, source: PipelineContext
) -> PipelineContext:
    """Commit a successfully completed isolated plugin context to its caller."""
    target.request = source.request
    target.trace_id = source.trace_id
    target.response = source.response
    target.should_stop = source.should_stop
    target.should_stream = source.should_stream
    target.request_id = source.request_id
    target.user_id = source.user_id
    target.extra = source.extra
    target.pipeline_kind = source.pipeline_kind
    target.is_multimodal = source.is_multimodal
    target.total_token_savings = source.total_token_savings
    target.request_context = source.request_context
    if hasattr(source, "_skip_names"):
        target._skip_names = set(source._skip_names)
    elif hasattr(target, "_skip_names"):
        del target._skip_names
    return target


def _plugin_timeout(plugin: Plugin, ctx: PipelineContext) -> float:
    timeout = max(0.001, float(getattr(plugin, "timeout_seconds", 30.0)))
    if isinstance(ctx.request_context, RequestContext):
        timeout = min(timeout, ctx.request_context.remaining_seconds)
    if timeout <= 0:
        raise TimeoutError("request deadline exceeded before plugin execution")
    return timeout


def _track_orphaned_plugin_task(task: asyncio.Task[PipelineContext]) -> None:
    _ORPHANED_PLUGIN_TASKS.add(task)

    def _done(completed: asyncio.Task[PipelineContext]) -> None:
        _ORPHANED_PLUGIN_TASKS.discard(completed)
        try:
            completed.exception()
        except (Exception, asyncio.CancelledError):
            pass

    task.add_done_callback(_done)


async def _await_plugin_task(
    task: asyncio.Task[PipelineContext], timeout: float
) -> PipelineContext:
    try:
        async with asyncio.timeout(timeout):
            return await asyncio.shield(task)
    except (TimeoutError, asyncio.CancelledError):
        # Register even if completion raced with the deadline; the callback
        # consumes a terminal exception and prevents an unobserved-task warning.
        _track_orphaned_plugin_task(task)
        raise


async def _finish_task_before_resource_release(
    task: asyncio.Task[PipelineContext],
) -> PipelineContext:
    """Wait through repeated owner cancellation before releasing a GPU lease."""
    current = asyncio.current_task()
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()
            if current is not None:
                current.uncancel()


async def execute_plugin(plugin: Plugin, ctx: PipelineContext) -> PipelineContext:
    """Execute one plugin and detach non-cancellable work at its deadline."""
    task = asyncio.create_task(
        plugin.execute(ctx),
        name=f"plugin-{plugin.name}-{ctx.trace_id or 'request'}",
    )
    return await _await_plugin_task(task, _plugin_timeout(plugin, ctx))


class PipelineEngine:
    """异步插件管线引擎。"""

    def __init__(self, registry: PluginRegistry, pipeline_kind: str = "understanding") -> None:
        self.registry = registry
        self.pipeline_kind = pipeline_kind
        self._ordered_plugins: list[Plugin] = []
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

    async def execute(self, request: dict[str, Any]) -> dict[str, Any]:
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
                skip_names = getattr(ctx, "_skip_names", set())
                if plugin.name in skip_names:
                    continue
                if ctx.should_stop:
                    collector = TraceCollector.current()
                    if collector:
                        # kind=plugin 事件只记耗时+状态(无 payload,debug 关闭也显示)
                        collector.emit(TraceEvent(
                            trace_id=ctx.trace_id,
                            ts=time.monotonic(),
                            stage=plugin.name,
                            kind="plugin",
                            name=f"{plugin.name}.skip",
                            duration_ms=0.0,  # 被跳过，未实际执行
                            status="skip",
                            payload=None,
                        ))
                        # skip 原因走 debug 维度(仅 debug 开启时显示)
                        collector.emit_debug(
                            stage=plugin.name, name=f"{plugin.name}.skip",
                            duration_ms=0.0, status="skip", dimension="plugin",
                            payload={"reason": "should_stop"},
                        )
                    logger.debug(
                        "插件 %s 被跳过 (should_stop=True, request_id=%s)",
                        plugin.name,
                        ctx.request_id,
                    )
                    continue

                plugin_name = plugin.name
                plugin_start = time.monotonic()

                try:
                    lifecycle_owner = getattr(plugin, "_strategy", plugin)
                    coordinator = getattr(self, "gpu_coordinator", None)
                    requested_device = getattr(
                        lifecycle_owner, "gpu_device_request", None
                    )
                    caller_ctx = ctx
                    plugin_ctx = _clone_pipeline_context(caller_ctx)
                    if (
                        coordinator is not None
                        and coordinator.config.enabled
                        and requested_device is not None
                    ):
                        async def _execute_with_gpu_lease(
                            *,
                            bound_coordinator: Any = coordinator,
                            component_name: str = plugin_name,
                            device_request: str = str(requested_device),
                            owner: Any = lifecycle_owner,
                            plugin_instance: Plugin = plugin,
                            context: PipelineContext = plugin_ctx,
                        ) -> PipelineContext:
                            async with bound_coordinator.gateway_lease(
                                component_name, device_request
                            ) as gpu_lease:
                                set_device = getattr(
                                    owner, "set_runtime_device", None
                                )
                                if callable(set_device):
                                    set_device(gpu_lease.device)
                                plugin_task = asyncio.create_task(
                                    plugin_instance.execute(context),
                                    name=(
                                        f"plugin-work-{component_name}-"
                                        f"{context.trace_id or 'request'}"
                                    ),
                                )
                                try:
                                    return await asyncio.shield(plugin_task)
                                except asyncio.CancelledError:
                                    # A lost distributed lease cancels this owner.
                                    # Repeated shutdown/heartbeat cancellation must
                                    # not release local GPU state while an
                                    # uncancellable worker thread is still active.
                                    await _finish_task_before_resource_release(
                                        plugin_task
                                    )
                                    raise

                        task = asyncio.create_task(
                            _execute_with_gpu_lease(),
                            name=(
                                f"plugin-gpu-{plugin_name}-"
                                f"{ctx.trace_id or 'request'}"
                            ),
                        )
                        plugin_result = await _await_plugin_task(
                            task, _plugin_timeout(plugin, caller_ctx)
                        )
                    else:
                        plugin_result = await execute_plugin(plugin, plugin_ctx)
                    ctx = (
                        _merge_pipeline_context(caller_ctx, plugin_result)
                        if plugin_result is plugin_ctx
                        else plugin_result
                    )
                except Exception as exc:
                    elapsed_ms = (time.monotonic() - plugin_start) * 1000
                    collector = TraceCollector.current()
                    if collector:
                        # error 事件:耗时+状态始终显示,错误原因走 debug 维度
                        collector.emit(TraceEvent(
                            trace_id=ctx.trace_id,
                            ts=time.monotonic(),
                            stage=plugin_name,
                            kind="plugin",
                            name=f"{plugin_name}.execute",
                            duration_ms=round(elapsed_ms, 2),
                            status="error",
                            payload=None,
                        ))
                        collector.emit_debug(
                            stage=plugin_name, name=f"{plugin_name}.execute",
                            duration_ms=round(elapsed_ms, 2), status="error",
                            dimension="plugin",
                            payload={"reason": _sanitize_exc(exc, 500)},
                        )
                    logger.error(
                        "插件 %s 执行失败: %s, request_id=%s",
                        plugin_name,
                        exc,
                        ctx.request_id,
                    )
                    if getattr(plugin, "failure_policy", "continue") == "fail_fast":
                        ctx.should_stop = True
                        ctx.extra.setdefault("pipeline_error", _sanitize_exc(exc, 500))
                        break
                    continue

                elapsed_ms = (time.monotonic() - plugin_start) * 1000
                collector = TraceCollector.current()
                if collector:
                    # ok 事件:耗时+状态始终显示,无 payload
                    collector.emit(TraceEvent(
                        trace_id=ctx.trace_id,
                        ts=time.monotonic(),
                        stage=plugin_name,
                        kind="plugin",
                        name=f"{plugin_name}.execute",
                        duration_ms=round(elapsed_ms, 2),
                        status="ok",
                        payload=None,
                    ))
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

    def _topological_sort(self, plugins: list[Plugin]) -> list[Plugin]:
        name_to_plugin: dict[str, Plugin] = {plugin.name: plugin for plugin in plugins}
        in_degree: dict[str, int] = {plugin.name: 0 for plugin in plugins}
        dependents: dict[str, list[str]] = {plugin.name: [] for plugin in plugins}

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

        queue: list[str] = []
        for name, degree in in_degree.items():
            if degree == 0:
                queue.append(name)

        ordered_names: list[str] = []
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

    def _build_response(self, ctx: PipelineContext) -> dict[str, Any]:
        response_data: dict[str, Any] = {}

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

    def _build_error_response(self, message: str) -> dict[str, Any]:
        return {
            "error": {
                "code": "internal_error",
                "message": f"Internal gateway error: {message}",
            }
        }


__all__ = ["PipelineEngine", "Plugin"]
