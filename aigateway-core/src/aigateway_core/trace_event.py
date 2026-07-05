"""统一 TraceEvent 通道 —— 按 trace_id 累积事件,请求结束落 Redis.

三件事(trace_id 全链路 / debug 开关 / 控制台分栏)共享这条通道:
- trace_id 那件事 = 修 mint 点 + 所有埋点统一进 collector
- debug 那件事 = collector 决定要不要收 kind=debug 事件 + 填 payload
- 控制台分栏 = 纯前端,但 trace 详情弹窗复用同一份数据
"""
from __future__ import annotations

import json
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal, Optional


@dataclass
class TraceEvent:
    """单条 trace 事件."""
    trace_id: str
    ts: float                                  # time.monotonic(),用于排序
    stage: str                                 # "auth"|"dispatch"|"pii"|"media"|"cache"|"bridge"|"quota"|"compress"|插件名
    kind: Literal["stage", "plugin", "debug"]
    name: str                                  # 如 "prompt_cache.lookup" / "pii_detector.sanitize"
    duration_ms: Optional[float]
    status: Literal["ok", "skip", "error"]
    payload: Optional[dict[str, Any]] = None   # 仅 debug 事件或对应开关开时填


class TraceCollector:
    """进程内按 trace_id 累积事件,请求结束 flush 到 Redis.

    用 ContextVar 隔离并发请求 —— 同一 async 任务链上所有代码都能通过
    TraceCollector.current() 拿到当前请求的 collector。
    """

    _current: ContextVar[Optional["TraceCollector"]] = ContextVar(
        "trace_collector", default=None
    )

    def __init__(self, trace_id: str) -> None:
        self.trace_id = trace_id
        self.events: list[TraceEvent] = []
        self._wall_start = time.time()

    @classmethod
    def current(cls) -> Optional["TraceCollector"]:
        return cls._current.get()

    @classmethod
    def start(cls, trace_id: str) -> "TraceCollector":
        c = cls(trace_id)
        cls._current.set(c)
        return c

    def emit(self, ev: TraceEvent) -> None:
        self.events.append(ev)

    def emit_debug(self, stage: str, name: str, duration_ms: float,
                   status: str, dimension: str, payload: dict[str, Any] | None) -> None:
        """发 kind=debug 事件 —— 仅当对应维度开关开启时才发且填 payload.

        Args:
            stage: 与对应 kind=stage/plugin 事件同 stage(便于关联)
            name: 同上,具体动作名
            duration_ms: 同上,耗时
            status: "ok"|"skip"|"error"
            dimension: "entry"|"cache"|"bridge"|"plugin" —— 决定查哪个开关
            payload: debug 详情(开关关时被忽略,只发 stage 事件本身的耗时)
        """
        from aigateway_core.debug_config import get_debug_config
        cfg = get_debug_config()
        if dimension == "entry":
            enabled = cfg.entry
        elif dimension == "cache":
            enabled = cfg.cache
        elif dimension == "bridge":
            enabled = cfg.bridge
        elif dimension == "plugin":
            enabled = cfg.is_plugin_debug(stage)
        else:
            enabled = False
        if not enabled:
            return
        import time as _time
        self.emit(TraceEvent(
            trace_id=self.trace_id, ts=_time.monotonic(),
            stage=stage, kind="debug", name=name,
            duration_ms=round(duration_ms, 2) if duration_ms is not None else None,
            status=status, payload=payload,
        ))

    def to_dict(self) -> dict[str, Any]:
        """序列化为可写 Redis 的字典."""
        return {
            "trace_id": self.trace_id,
            "wall_start": self._wall_start,
            "events": [
                {
                    "ts": ev.ts,
                    "stage": ev.stage,
                    "kind": ev.kind,
                    "name": ev.name,
                    "duration_ms": round(ev.duration_ms, 2) if ev.duration_ms is not None else None,
                    "status": ev.status,
                    "payload": ev.payload,
                }
                for ev in self.events
            ],
        }

    async def flush(self, redis_client: Any) -> None:
        """请求结束时调用,写 Redis hash aigateway:trace:{trace_id}.

        Args:
            redis_client: 异步 Redis 客户端(fakeredis 或真实 redis.asyncio)。
        """
        if redis_client is None:
            return
        key = f"aigateway:trace:{self.trace_id}"
        value = json.dumps(self.to_dict())
        await redis_client.hset(key, "data", value)
        await redis_client.expire(key, 7 * 24 * 3600)  # TTL 7 天
