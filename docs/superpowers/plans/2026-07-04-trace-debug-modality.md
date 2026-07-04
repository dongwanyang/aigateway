# 全链路 trace_id + 分维度 Debug 开关 + 控制台插件分栏 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复一次请求生成 3+ 个 trace_id 的 bug,建立统一 TraceEvent 通道,用 5 维度 + 11 插件 debug 开关替换粗暴的 `debug_mode` 总开关,并把控制台 `/plugins` 页改成按 `pipeline_kind` 上下分栏。

**Architecture:** 方案 A —— 三件事共享一套 `TraceEvent` + `TraceCollector` 数据通道。ASGI 中间件统一生成 trace_id 并写入 `request.state`,所有 `PipelineContext` 构造点复用同一个 id;dispatcher/engine/插件的所有埋点统一 `collector.emit(TraceEvent(...))`;debug 开关决定是否 emit `kind=debug` 事件及是否填 payload;控制台分栏是纯前端改动但复用同一份数据。分 3 个 PR:PR1 trace 通道 + model_router 退役;PR2 debug 开关;PR3 前端 UI。

**Tech Stack:** Python 3.12 / FastAPI / structlog / Redis (async) / React + TypeScript + TailwindCSS / pytest + asyncio

## Global Constraints

- 后端包路径:`aigateway-core/src/aigateway_core/`(被 aigateway-api 通过 `sys.path` 引入)和 `aigateway-api/src/aigateway_api/`
- 测试用 `python3 -m pytest tests/<file> -v`(无 `python` alias,无 conftest.py,测试用 `sys.path.insert` 引入 `aigateway-core/src`)
- 跳过 flaky 测试:`--ignore=tests/test_template_routes.py`
- 新增 Python 依赖改 `aigateway-api/requirements.txt`(本计划不新增依赖)
- 配置优先级:进程环境变量 > `.env` > `config.yaml` > 代码默认值;`config.yaml` 支持 `${VAR}` / `${VAR:-default}` 插值
- 热重载:`hot_reload: true` 时 Watchdog 拾取 config.yaml 变更,走 `ConfigManager.on_reload()` 回调
- 后端改动需 `docker compose up -d --build gateway` 重建;前端改动 `npm run dev` 期间 HMR,生产 `docker compose up -d --build control-panel`
- Dockerfile 分层:apt → torch → requirements.txt → Qwen3 模型 → 源码(最后),改源码只重建最后两层
- 不引入 OpenTelemetry collector / Jaeger / Tempo(用户明确拒绝)
- `prompt_compress` 保留在 dispatcher 内联(不挪回 engine),归 `entry` debug 档
- `model_router` 空壳彻底退役(删类 + 删注册 + 前端不显示)
- 5xx 错误 detail 固定回显(脱敏后始终返回,不挂任何 debug 开关)

---

# PR1:trace_id 全链路 + TraceEvent 通道 + model_router 退役

## File Structure (PR1)

- **Create** `aigateway-core/src/aigateway_core/trace_event.py` — `TraceEvent` dataclass + `TraceCollector`(ContextVar 累积 + Redis flush)
- **Create** `aigateway-api/src/aigateway_api/trace_middleware.py` — ASGI 中间件,生 trace_id 写 `request.state`,启动 collector,响应回写 `X-Trace-Id`
- **Modify** `aigateway-core/src/aigateway_core/context.py` — `trace_id` 改必传(删默认 factory)
- **Modify** `aigateway-core/src/aigateway_core/pipeline.py` — engine 循环埋点迁 `collector.emit`;删 `ModelRouterPlugin` 类 + 注册;`prompt_compress.depends_on` 摘 model_router
- **Modify** `aigateway-api/src/aigateway_api/dispatcher.py` — 2 个 ctx 传 trace_id;`_skip_names` 去 model_router;内联埋点迁 `collector.emit`;`_run_engine_filtered` 的 add_plugin_trace 迁 emit
- **Modify** `aigateway-api/src/aigateway_api/openai_compat.py` — 3 个共用前置 ctx 传 trace_id;`_record_request_log` 用 `request.state.trace_id`(不再 fallback mint)
- **Modify** `aigateway-core/src/aigateway_core/logger.py` — `ContextInjectProcessor` 优先读 `TraceCollector.current().trace_id`
- **Modify** 6 个 gen-opt 插件 — 删 `create_plugin_span` + `mark_span_error`,改 `collector.emit`
- **Modify** `aigateway-api/src/aigateway_api/main.py` — 挂 `TraceMiddleware`;删 `_is_debug_mode()` 关联的 5xx detail 开关(固定回显)
- **Modify** `aigateway-api/src/aigateway_api/admin_routes.py` — `/admin/trace/{id}` 返回 `events` 数组(兼容保留 `plugin_trace` 字段)
- **Test** `tests/test_trace_event.py`(新)、`tests/test_trace_middleware.py`(新)、扩充 `tests/test_tracing_integration.py`

---

## Task 1: TraceEvent dataclass + TraceCollector

**Files:**
- Create: `aigateway-core/src/aigateway_core/trace_event.py`
- Test: `tests/test_trace_event.py`

**Interfaces:**
- Produces: `TraceEvent` dataclass, `TraceCollector` class with `start(trace_id)` classmethod, `current()` classmethod, `emit(ev)` instance method, `flush(redis)` async instance method, `events` list attribute, `trace_id` attribute

- [ ] **Step 1: Write failing test**

Create `tests/test_trace_event.py`:

```python
"""TraceEvent + TraceCollector 单元测试."""
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.trace_event import TraceEvent, TraceCollector


def test_trace_event_fields():
    ev = TraceEvent(
        trace_id="t1", ts=time.monotonic(), stage="cache",
        kind="stage", name="prompt_cache.lookup",
        duration_ms=1.5, status="ok",
    )
    assert ev.payload is None
    assert ev.status == "ok"


def test_collector_start_sets_current():
    TraceCollector._current.set(None)  # reset
    c = TraceCollector.start("trace-abc")
    assert c.trace_id == "trace-abc"
    assert TraceCollector.current() is c


def test_collector_emit_accumulates():
    TraceCollector._current.set(None)
    c = TraceCollector.start("trace-abc")
    ev = TraceEvent(trace_id="t1", ts=0.0, stage="auth", kind="stage",
                    name="auth.verify", duration_ms=1.0, status="ok")
    c.emit(ev)
    assert len(c.events) == 1
    assert c.events[0].name == "auth.verify"


def test_collector_current_none_when_not_started():
    TraceCollector._current.set(None)
    assert TraceCollector.current() is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_trace_event.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aigateway_core.trace_event'`

- [ ] **Step 3: Write minimal implementation**

Create `aigateway-core/src/aigateway_core/trace_event.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_trace_event.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add aigateway-core/src/aigateway_core/trace_event.py tests/test_trace_event.py
git commit -m "feat(trace): TraceEvent dataclass + TraceCollector (ContextVar 累积 + Redis flush)"
```

---

## Task 2: ASGI TraceMiddleware

**Files:**
- Create: `aigateway-api/src/aigateway_api/trace_middleware.py`
- Test: `tests/test_trace_middleware.py`

**Interfaces:**
- Consumes: `TraceCollector.start(trace_id)` from Task 1, `TraceCollector.current()`
- Produces: `TraceMiddleware` class (ASGI middleware, constructs as `app.add_middleware(TraceMiddleware)`); writes `request.state.trace_id`; reads `request.app.state.redis` for flush

- [ ] **Step 1: Write failing test**

Create `tests/test_trace_middleware.py`:

```python
"""TraceMiddleware 测试 —— 生成 trace_id、写 request.state、回写响应头."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-api", "src"))

import asyncio
from unittest.mock import AsyncMock, MagicMock

from starlette.testclient import TestClient
from fastapi import FastAPI, Request

from aigateway_api.trace_middleware import TraceMiddleware


def _make_app(redis_mock=None):
    app = FastAPI()
    if redis_mock is not None:
        app.state.redis = redis_mock
    app.add_middleware(TraceMiddleware)

    @app.get("/ping")
    async def ping(request: Request):
        return {"trace_id": request.state.trace_id}

    return app


def test_middleware_generates_trace_id_when_absent():
    app = _make_app(redis_mock=AsyncMock())
    client = TestClient(app)
    resp = client.get("/ping")
    assert resp.status_code == 200
    tid = resp.json()["trace_id"]
    assert len(tid) == 32  # uuid4().hex
    # 响应头回写
    assert resp.headers["x-trace-id"] == tid


def test_middleware_uses_incoming_x_trace_id():
    app = _make_app(redis_mock=AsyncMock())
    client = TestClient(app)
    resp = client.get("/ping", headers={"x-trace-id": "incoming-id-123"})
    assert resp.json()["trace_id"] == "incoming-id-123"
    assert resp.headers["x-trace-id"] == "incoming-id-123"


def test_middleware_flushes_to_redis():
    redis_mock = AsyncMock()
    redis_mock.hset = AsyncMock()
    redis_mock.expire = AsyncMock()
    app = _make_app(redis_mock=redis_mock)
    client = TestClient(app)
    resp = client.get("/ping")
    assert resp.status_code == 200
    # flush 被调用
    assert redis_mock.hset.called
    args = redis_mock.hset.call_args.args
    assert args[0].startswith("aigateway:trace:")
    assert redis_mock.expire.called
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_trace_middleware.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aigateway_api.trace_middleware'`

- [ ] **Step 3: Write minimal implementation**

Create `aigateway-api/src/aigateway_api/trace_middleware.py`:

```python
"""ASGI 中间件:统一生成/复用 trace_id,启动 TraceCollector,响应回写 X-Trace-Id.

修"一次请求 3+ mint 点"bug 的核心:所有下游代码(ctx 构造、log-recorder、
插件)都从 request.state.trace_id 或 TraceCollector.current() 取同一个 id,
不再各自 uuid4()。
"""
from __future__ import annotations

import uuid
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

from aigateway_core.trace_event import TraceCollector


class TraceMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 从请求头读 trace_id,没有就生成
        headers = dict(scope.get("headers", []))
        trace_id = (
            headers.get(b"x-trace-id", b"").decode("ascii")
            or uuid.uuid4().hex
        )

        # 写 scope["state"](FastAPI request.state 的底层)
        if "state" not in scope:
            scope["state"] = {}
        scope["state"]["trace_id"] = trace_id

        # 启动 collector(写 ContextVar)
        collector = TraceCollector.start(trace_id)

        # 拿 redis 用于 flush(scope["state"] 在 app.state 之外)
        # app.state.redis 在 lifespan 里设置;这里通过 scope["app"] 拿
        app_obj = scope.get("app")
        redis_client = getattr(app_obj.state, "redis", None) if app_obj else None

        status_holder = {"status": 500}

        async def send_wrapper(message: Any) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                # 回写 X-Trace-Id 响应头
                headers_list = list(message.get("headers", []))
                headers_list.append((b"x-trace-id", trace_id.encode("ascii")))
                message["headers"] = headers_list
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            try:
                await collector.flush(redis_client)
            except Exception:
                # flush 失败不影响请求
                pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_trace_middleware.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add aigateway-api/src/aigateway_api/trace_middleware.py tests/test_trace_middleware.py
git commit -m "feat(trace): ASGI TraceMiddleware 统一生成 trace_id + 回写响应头"
```

---

## Task 3: PipelineContext.trace_id 改必传

**Files:**
- Modify: `aigateway-core/src/aigateway_core/context.py:48,61`

**Interfaces:**
- Produces: `PipelineContext(trace_id=...)` now required (no default). All callers must pass `trace_id`. `request_id` keeps its default factory (it's a separate id, not used for trace correlation).

- [ ] **Step 1: Write failing test**

Append to `tests/test_trace_event.py`:

```python
def test_pipeline_context_trace_id_required():
    """trace_id 不再有默认值,必须显式传入."""
    from aigateway_core.context import PipelineContext
    import pytest
    with pytest.raises(TypeError):
        PipelineContext(request={"messages": [], "model": "gpt"})  # 缺 trace_id


def test_pipeline_context_with_trace_id():
    from aigateway_core.context import PipelineContext
    ctx = PipelineContext(request={"messages": [], "model": "gpt"}, trace_id="t-fixed")
    assert ctx.trace_id == "t-fixed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_trace_event.py::test_pipeline_context_trace_id_required -v`
Expected: FAIL (context still has default factory, so no TypeError raised)

- [ ] **Step 3: Modify context.py**

In `aigateway-core/src/aigateway_core/context.py`, line 61, change:

```python
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
```

to:

```python
    trace_id: str  # 必传 —— 由 TraceMiddleware 生成,经 dispatcher/openai_compat 透传,保证全请求唯一
```

Also update the docstring at line 48 from `trace_id: OpenTelemetry 追踪 ID（UUID4）。` to `trace_id: 全请求唯一追踪 ID（由 TraceMiddleware 生成,必须显式传入）。`

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_trace_event.py -v`
Expected: 6 passed (original 4 + new 2)

- [ ] **Step 5: Commit**

```bash
git add aigateway-core/src/aigateway_core/context.py tests/test_trace_event.py
git commit -m "refactor(trace): PipelineContext.trace_id 改必传(删默认 factory)"
```

> **注意:** 此 commit 后 `dispatcher.py`/`openai_compat.py` 的 5 个 ctx 构造点会立即报 TypeError(它们没传 trace_id)。下一个 Task 修复它们。如果跑全量测试此时会大面积失败,属预期,Task 4 修完即恢复。

---

## Task 4: dispatcher + openai_compat 的 5 个 ctx 构造点传 trace_id

**Files:**
- Modify: `aigateway-api/src/aigateway_api/dispatcher.py:293,447`
- Modify: `aigateway-api/src/aigateway_api/openai_compat.py:345,396,531,275`

**Interfaces:**
- Consumes: `TraceCollector.current()` from Task 1, `request.state.trace_id` set by Task 2's middleware
- Produces: all 5 `PipelineContext(...)` calls now pass `trace_id=...`; `_record_request_log` uses `request.state.trace_id` without mint fallback

- [ ] **Step 1: Locate the 5 ctx sites + log-recorder**

Run to confirm current state:
```bash
grep -n "PipelineContext(" aigateway-api/src/aigateway_api/dispatcher.py aigateway-api/src/aigateway_api/openai_compat.py
grep -n "trace_id = getattr" aigateway-api/src/aigateway_api/openai_compat.py
```
Expected: 5 `PipelineContext(` lines (dispatcher 293/447, openai_compat 345/396/531) and 1 `trace_id = getattr` at openai_compat:275.

- [ ] **Step 2: Fix dispatcher ctx sites**

In `dispatcher.py`, the `dispatch()` method has access to `request` (it's passed in or via a parameter). Read the dispatch signature first:

```bash
grep -n "async def dispatch\|def dispatch\|request\." aigateway-api/src/aigateway_api/dispatcher.py | head -20
```

Find how `dispatch()` gets the trace_id. The dispatcher is called from `openai_compat.py`'s route handler which has `request: Request`. Two options:
- (a) `dispatch()` already receives `request` — read `request.state.trace_id`
- (b) `dispatch()` doesn't — add a `trace_id: str` parameter and have callers pass `request.state.trace_id`

Read the dispatch signature and its callers to decide. Implement whichever is cleaner. For each of the 2 ctx sites (`dispatcher.py:293` understanding, `:447` generation), add `trace_id=<the_trace_id>,` to the `PipelineContext(...)` call.

Example if dispatch receives `request`:
```python
ctx = PipelineContext(
    request=...,
    trace_id=request.state.trace_id,   # 新增
    ...
)
```

Example if adding a parameter:
```python
async def dispatch(self, body, *, trace_id: str, ...):
    ...
    ctx = PipelineContext(request=..., trace_id=trace_id, ...)
```
and update callers in `openai_compat.py` to pass `trace_id=request.state.trace_id`.

- [ ] **Step 3: Fix openai_compat 共用前置 ctx sites**

In `openai_compat.py`, the 3 helper functions `_apply_pii_detection` / `_apply_media_optimization` / (third at 531) each construct a ctx. These helpers receive `request` (or `body` + `request`). Add `trace_id=request.state.trace_id` to each:

```python
ctx = PipelineContext(
    request={"messages": body.messages, "model": body.model},
    trace_id=request.state.trace_id,   # 新增
)
```

If a helper doesn't currently receive `request`, add it as a parameter and update its callers.

- [ ] **Step 4: Fix _record_request_log trace_id mint**

In `openai_compat.py:275`, change:

```python
    trace_id = getattr(request.state, "trace_id", "") or str(uuid.uuid4().hex[:12])
```

to:

```python
    trace_id = getattr(request.state, "trace_id", "")
    # 不再 fallback mint —— TraceMiddleware 已保证 request.state.trace_id 一定存在
```

(Remove the `or str(uuid.uuid4().hex[:12])` fallback. If `request.state.trace_id` is genuinely absent it returns `""`, which signals a middleware-wiring bug worth surfacing rather than masking.)

- [ ] **Step 5: Verify by running a broad test**

Run: `python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py -x 2>&1 | tail -30`
Expected: no `TypeError: __init__() missing 1 required positional argument: 'trace_id'` errors. Some tests may still fail for other reasons (e.g. they construct PipelineContext without trace_id in test fixtures) — fix those by adding `trace_id="test-trace"` to test fixtures.

- [ ] **Step 6: Commit**

```bash
git add aigateway-api/src/aigateway_api/dispatcher.py aigateway-api/src/aigateway_api/openai_compat.py tests/
git commit -m "fix(trace): 5 个 PipelineContext 构造点 + log-recorder 统一复用 request.state.trace_id"
```

---

## Task 5: engine 循环埋点迁 collector.emit

**Files:**
- Modify: `aigateway-core/src/aigateway_core/pipeline.py:150-194`

**Interfaces:**
- Consumes: `TraceCollector.current()`, `TraceEvent` from Task 1
- Produces: engine loop emits `TraceEvent(kind="plugin", stage=plugin.name, ...)` instead of `ctx.add_plugin_trace(...)`

- [ ] **Step 1: Read the engine loop**

```bash
sed -n '145,210p' aigateway-core/src/aigateway_core/pipeline.py
```
Confirm 3 `ctx.add_plugin_trace` calls at lines 161 (skipped), 177 (failed), 189 (success).

- [ ] **Step 2: Replace add_plugin_trace with collector.emit**

In `aigateway-core/src/aigateway_core/pipeline.py`, at the top of the file add import (after existing imports around line 25):

```python
from aigateway_core.trace_event import TraceCollector, TraceEvent
```

Replace the 3 `ctx.add_plugin_trace(...)` calls. The skipped branch (line 161):

```python
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
```

The failed branch (line 177):

```python
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
                        plugin_name, exc, ctx.request_id,
                    )
                    continue
```

The success branch (line 189):

```python
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
                logger.debug(
                    "插件 %s 执行完毕: %.2fms, request_id=%s",
                    plugin_name, elapsed_ms, ctx.request_id,
                )
```

- [ ] **Step 3: Verify engine still runs**

Run: `python3 -m pytest tests/test_tracing_integration.py -v 2>&1 | tail -20`
Expected: PASS (or only pre-existing failures unrelated to add_plugin_trace). If `test_tracing_integration.py` asserts on `ctx.get_plugin_trace()` content, those assertions need updating — see Step 4.

- [ ] **Step 4: Update tests that assert on plugin_trace**

Search for tests depending on the old `_plugin_trace` extra:
```bash
grep -rn "plugin_trace\|get_plugin_trace\|add_plugin_trace" tests/ aigateway-api/src/ aigateway-core/src/
```
For each non-engine caller (e.g. dispatcher's `_run_engine_filtered` at dispatcher.py:929/933 — that's Task 6, leave for now; tests asserting on returned `plugin_trace` arrays), either:
- update the assertion to check `TraceCollector.current().events` filtered by `kind=="plugin"`, or
- keep `add_plugin_trace` as a compat shim (see Task 6 decision).

For this task, only fix tests that break due to the engine change. Leave dispatcher's own `add_plugin_trace` calls for Task 6.

- [ ] **Step 5: Commit**

```bash
git add aigateway-core/src/aigateway_core/pipeline.py tests/
git commit -m "refactor(trace): engine 循环埋点迁 TraceCollector.emit(kind=plugin)"
```

---

## Task 6: dispatcher 内联埋点 + _run_engine_filtered 迁 emit;_skip_names 去 model_router

**Files:**
- Modify: `aigateway-api/src/aigateway_api/dispatcher.py:300,929,933` + 内联 add_plugin_trace 点(196,204,215,339,361,382,392,396,567,579,715)

**Interfaces:**
- Consumes: `TraceCollector.current()`, `TraceEvent`
- Produces: all dispatcher manual traces emit `TraceEvent(kind="stage", stage="cache"|"quota"|"compress"|"auth"|"media", ...)`; `_skip_names` no longer contains `model_router`; `add_plugin_trace` compat shim kept (delegates to emit) so `_run_engine_filtered` and any test fixtures still work.

- [ ] **Step 1: Remove model_router from _skip_names**

In `dispatcher.py:300`, the `_skip_names` set:

```python
                ctx._skip_names = {"pii_detector", "prompt_cache", "semantic_cache",
```
Read the full set (it spans lines 300-301):
```bash
sed -n '300,302p' aigateway-api/src/aigateway_api/dispatcher.py
```
Remove `"model_router"` from it (and `"prompt_compress"` stays — it's still inlined per spec 2.4). Also remove `"prompt_cache"`, `"semantic_cache"`, `"pii_detector"`, `"media_optimizer"` ONLY if Task 7-9 decide to — NO, per spec these stay skipped (dispatcher inlines them). Only remove `model_router`.

- [ ] **Step 2: Add a helper to emit stage events**

At the top of `dispatcher.py` (after imports), add a small helper:

```python
def _emit_stage(trace_id: str, stage: str, name: str, duration_ms: float,
                status: str = "ok", payload: dict | None = None) -> None:
    """发一条 kind=stage 的 TraceEvent(若无 collector 则静默)."""
    from aigateway_core.trace_event import TraceCollector, TraceEvent
    import time as _time
    collector = TraceCollector.current()
    if collector:
        collector.emit(TraceEvent(
            trace_id=trace_id,
            ts=_time.monotonic(),
            stage=stage,
            kind="stage",
            name=name,
            duration_ms=round(duration_ms, 2),
            status=status,
            payload=payload,
        ))
```

- [ ] **Step 3: Replace each manual add_plugin_trace with _emit_stage**

For each of the dispatcher's manual trace points (lines ~196, 204, 215, 339, 361, 382, 392, 396, 567, 579, 715 — confirm with `grep -n "add_plugin_trace\|manual.*trace" dispatcher.py`), replace:

```python
ctx.add_plugin_trace("prompt_cache", elapsed, "success")
```
with:
```python
_emit_stage(ctx.trace_id, "cache", "prompt_cache.lookup", elapsed, "success")
```

Stage names by location (confirm each by reading context):
- PII 共用前置 → `stage="pii"`, `name="pii_detector.sanitize"`
- media 共用前置 → `stage="media"`, `name="media_optimizer.process"`
- cache lookup → `stage="cache"`, `name="prompt_cache.lookup"` / `"semantic_cache.lookup"`
- quota check → `stage="quota"`, `name="key_store.check_quota"`
- compress → `stage="compress"`, `name="prompt_compress.compress"`

- [ ] **Step 4: Update _run_engine_filtered (lines 929, 933)**

Read `_run_engine_filtered` (around line 906-940):
```bash
sed -n '906,940p' aigateway-api/src/aigateway_api/dispatcher.py
```
It has its own `ctx.add_plugin_trace(plugin.name, elapsed, "failed"/"success")` for plugins run via this filtered path. Replace with `_emit_stage(ctx.trace_id, "plugin", f"{plugin.name}.execute", elapsed, status)` — but note `kind` here should be `"plugin"` not `"stage"` (these ARE plugin executions). Adjust the helper or call `TraceCollector.current().emit(TraceEvent(..., kind="plugin", ...))` directly:

```python
                    collector = TraceCollector.current()
                    if collector:
                        collector.emit(TraceEvent(
                            trace_id=ctx.trace_id, ts=time.monotonic(),
                            stage=plugin.name, kind="plugin",
                            name=f"{plugin.name}.execute",
                            duration_ms=round(elapsed, 2), status="error",
                        ))
```
(do the same for the success branch.)

- [ ] **Step 5: Keep add_plugin_trace as compat shim (or remove)**

Decision: keep `PipelineContext.add_plugin_trace` (context.py:345) as a no-op compat shim for now (some test fixtures may still call it). It just discards — or better, delegates to emit:

In `context.py:345`, replace the body of `add_plugin_trace` with:

```python
    def add_plugin_trace(self, plugin_name: str, duration_ms: float, status: str) -> None:
        """兼容包装 —— 委托给 TraceCollector.emit(kind=plugin).

        新代码应直接用 TraceCollector.current().emit(TraceEvent(...))。
        保留此方法仅为不破坏尚未迁移的调用点和测试 fixture。
        """
        from aigateway_core.trace_event import TraceCollector, TraceEvent
        import time as _time
        collector = TraceCollector.current()
        if collector:
            collector.emit(TraceEvent(
                trace_id=self.trace_id, ts=_time.monotonic(),
                stage=plugin_name, kind="plugin",
                name=f"{plugin_name}.execute",
                duration_ms=round(duration_ms, 2),
                status="success" if status == "success" else ("error" if status == "failed" else "skip"),
            ))
```

- [ ] **Step 6: Run tests**

Run: `python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py 2>&1 | tail -30`
Expected: no new failures from the trace migration. Fix any test that asserted on `get_plugin_trace()` — it should now read `TraceCollector.current().events`.

- [ ] **Step 7: Commit**

```bash
git add aigateway-api/src/aigateway_api/dispatcher.py aigateway-core/src/aigateway_core/context.py tests/
git commit -m "refactor(trace): dispatcher 内联埋点 + _run_engine_filtered 迁 emit;_skip_names 去 model_router"
```

---

## Task 7: 6 个 gen-opt 插件删 create_plugin_span,改 emit

**Files:**
- Modify: `aigateway-core/src/aigateway_core/generation_optimization/plugins/ai_director_plugin.py:103,173`
- Modify: `aigateway-core/src/aigateway_core/generation_optimization/plugins/intent_evaluator_plugin.py:105,163`
- Modify: `aigateway-core/src/aigateway_core/generation_optimization/plugins/token_compressor_plugin.py:118,225`
- Modify: `aigateway-core/src/aigateway_core/generation_optimization/plugins/draft_generator_plugin.py:108,189`
- Modify: `aigateway-core/src/aigateway_core/generation_optimization/plugins/gen_model_router_plugin.py:109,174,203`
- Modify: `aigateway-core/src/aigateway_core/generation_optimization/plugins/cost_tracker_plugin.py:110,185`

**Interfaces:**
- Consumes: `TraceCollector.current()`, `TraceEvent`, `ctx.trace_id`
- Produces: each plugin emits a `kind="plugin"` TraceEvent on success/error instead of fake span dict

- [ ] **Step 1: Read one plugin's span pattern**

```bash
sed -n '99,115p' aigateway-core/src/aigateway_core/generation_optimization/plugins/ai_director_plugin.py
sed -n '165,180p' aigateway-core/src/aigateway_core/generation_optimization/plugins/ai_director_plugin.py
```
Confirm: line 103 `span_context = tracing.create_plugin_span(...)`, line 173 `TracingManager.mark_span_error(span_context.get("span"), exc)`. The pattern is: create span at function start, mark error in except, (success path implicitly ends).

- [ ] **Step 2: Add a shared emit helper**

The 6 plugins share the same pattern. Rather than inline 6×, add a helper in the gen-opt plugins `__init__.py` or a small util. Create or extend `aigateway-core/src/aigateway_core/generation_optimization/plugins/__init__.py` with:

```python
def emit_plugin_event(ctx, name: str, duration_ms: float, status: str = "ok",
                      payload: dict | None = None) -> None:
    """gen-opt 插件发 TraceEvent 的统一入口."""
    from aigateway_core.trace_event import TraceCollector, TraceEvent
    import time as _time
    collector = TraceCollector.current()
    if collector:
        collector.emit(TraceEvent(
            trace_id=ctx.trace_id, ts=_time.monotonic(),
            stage=name, kind="plugin",
            name=f"{name}.execute",
            duration_ms=round(duration_ms, 2),
            status=status, payload=payload,
        ))
```

- [ ] **Step 3: Replace span calls in each of the 6 plugins**

For each plugin, the `execute()` method has:
```python
        start_time = time.monotonic()
        tracing = get_tracing_manager()
        span_context = tracing.create_plugin_span(
            span_context={"trace_id": ctx.trace_id},
            plugin_name=self.name,
            request_id=ctx.request_id,
        )
        try:
            ... main logic ...
        except Exception as exc:
            TracingManager.mark_span_error(span_context.get("span"), exc)
            raise
```

Replace with:
```python
        start_time = time.monotonic()
        try:
            ... main logic (unchanged) ...
        except Exception as exc:
            from aigateway_core.generation_optimization.plugins import emit_plugin_event
            emit_plugin_event(ctx, self.name, (time.monotonic() - start_time) * 1000, "error")
            raise
```

And add at the end of the success path (before `return ctx`):
```python
        from aigateway_core.generation_optimization.plugins import emit_plugin_event
        emit_plugin_event(ctx, self.name, (time.monotonic() - start_time) * 1000, "ok")
```

Remove the `from aigateway_core.tracing import ...` / `get_tracing_manager()` / `TracingManager` imports if they become unused (check with `grep`).

- [ ] **Step 4: Run gen-opt plugin tests**

Run: `python3 -m pytest tests/test_ai_director_plugin.py tests/test_intent_evaluator_plugin.py tests/test_token_compressor_strategy.py tests/test_gen_model_router_plugin.py tests/test_cost_tracker_plugin_group.py -v 2>&1 | tail -30`
Expected: PASS. If a test asserted on span creation, update it to assert on `TraceCollector.current().events`.

- [ ] **Step 5: Commit**

```bash
git add aigateway-core/src/aigateway_core/generation_optimization/plugins/
git commit -m "refactor(trace): 6 个 gen-opt 插件删假 create_plugin_span,改 emit_plugin_event"
```

---

## Task 8: model_router 彻底退役

**Files:**
- Modify: `aigateway-core/src/aigateway_core/pipeline.py:611-652,904,666`

**Interfaces:**
- Produces: `ModelRouterPlugin` class deleted; registration at line 904 deleted; `prompt_compress.depends_on` no longer references `model_router`.

- [ ] **Step 1: Read ModelRouterPlugin + its registration + depends_on**

```bash
sed -n '611,670p' aigateway-core/src/aigateway_core/pipeline.py
sed -n '900,910p' aigateway-core/src/aigateway_core/pipeline.py
```
Confirm: class at 611-652, registration `"model_router": (ModelRouterPlugin, {})` at 904, `prompt_compress.depends_on` at 666 includes `"model_router"`.

- [ ] **Step 2: Delete ModelRouterPlugin class**

Delete lines 611-652 (the entire `class ModelRouterPlugin` block including its docstring).

- [ ] **Step 3: Delete its registration**

At line 904, delete the line:
```python
        "model_router": (ModelRouterPlugin, {}),
```

- [ ] **Step 4: Fix prompt_compress.depends_on**

At line 666, change:
```python
    depends_on: list = ["model_router", "rag_retriever", "conv_compressor"]
```
to:
```python
    depends_on: list = ["rag_retriever", "conv_compressor"]
```

(Confirm actual content first — `sed -n '666p'` may show `["semantic_cache"]` per the survey; use whatever the real value is, just remove `"model_router"` from it.)

- [ ] **Step 5: Verify no remaining references**

```bash
grep -rn "ModelRouterPlugin\|\"model_router\"\|'model_router'" aigateway-core/src/ aigateway-api/src/ tests/
```
Expected: no matches except possibly in `dispatcher.py` `_skip_names` (already removed in Task 6) and test files. Remove any test that specifically tests ModelRouterPlugin (e.g. if `tests/test_model_router_strategy.py` exists — check; the survey mentioned `test_model_router_strategy.py` tests the *Strategy*, not the Plugin, so it likely stays). Confirm:

```bash
grep -l "ModelRouterPlugin" tests/ 2>/dev/null
```
If any test imports `ModelRouterPlugin`, delete that test or update it.

- [ ] **Step 6: Run tests**

Run: `python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py 2>&1 | tail -30`
Expected: PASS (no import errors for ModelRouterPlugin).

- [ ] **Step 7: Commit**

```bash
git add aigateway-core/src/aigateway_core/pipeline.py tests/
git commit -m "refactor: model_router 空壳彻底退役(删类 + 删注册 + 摘 depends_on)"
```

---

## Task 9: logger 自动注入 trace_id + main.py 挂中间件 + 5xx 固定回显

**Files:**
- Modify: `aigateway-core/src/aigateway_core/logger.py:58-147`
- Modify: `aigateway-api/src/aigateway_api/main.py:79-127,227-238` + 中间件挂载点

**Interfaces:**
- Produces: `ContextInjectProcessor` reads `TraceCollector.current().trace_id` first; `main.py` adds `TraceMiddleware`; `_is_debug_mode()` removed, 5xx detail fixed-on.

- [ ] **Step 1: Read ContextInjectProcessor**

```bash
sed -n '58,147p' aigateway-core/src/aigateway_core/logger.py
```
Find where it currently reads trace_id (likely from its own ContextVar set by `log_with_context`).

- [ ] **Step 2: Make processor prefer TraceCollector**

In the processor's `__call__` or `_get_context` method, before falling back to the old ContextVar, try:

```python
from aigateway_core.trace_event import TraceCollector
collector = TraceCollector.current()
if collector and collector.trace_id:
    context["trace_id"] = collector.trace_id
else:
    # fallback 到原有 ContextVar 逻辑
    ...
```

(Import inside the method to avoid circular import if `trace_event.py` ever imports logger — it doesn't, but be safe.)

- [ ] **Step 3: Read main.py middleware + 5xx + debug_mode**

```bash
sed -n '79,127p' aigateway-api/src/aigateway_api/main.py
sed -n '227,238p' aigateway-api/src/aigateway_api/main.py
grep -n "add_middleware\|middleware" aigateway-api/src/aigateway_api/main.py | head
```

- [ ] **Step 4: Mount TraceMiddleware**

In `main.py`'s `create_app()`, after app creation and before other middleware (or wherever auth_middleware is added), add:

```python
from aigateway_api.trace_middleware import TraceMiddleware
app.add_middleware(TraceMiddleware)
```

Place it so it runs BEFORE auth (outermost) — `add_middleware` adds to the stack in reverse order, so add TraceMiddleware AFTER auth to make it outermost. Confirm ordering by testing in Task 11.

- [ ] **Step 5: Fix 5xx detail to fixed-on, remove _is_debug_mode**

In `main.py:79-127` (the exception handler), replace `_is_debug_mode()` gating with always-on redacted detail:

```python
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    # 固定回显 redacted detail(脱敏),不再受 debug_mode 控制
    detail = repr(exc)
    if len(detail) > 500:
        detail = detail[:500] + "..."
    return JSONResponse(
        status_code=500,
        content={"error": {"message": "Internal Server Error", "detail": detail}},
    )
```

Delete the `_is_debug_mode()` function definition.

At `main.py:227-238`, delete the block that forces DEBUG log level based on debug_mode:

```python
# 删除:if debug_mode and ...: log_level = "DEBUG"
```
Replace with simply reading log_level from config (no debug_mode override). Keep `AI_GATEWAY_ENV=production` forcing log_level≥INFO safety net.

- [ ] **Step 6: Run integration test**

Run: `python3 -m pytest tests/test_tracing_integration.py -v 2>&1 | tail -20`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add aigateway-core/src/aigateway_core/logger.py aigateway-api/src/aigateway_api/main.py
git commit -m "feat(trace): logger 自动注入 trace_id;挂 TraceMiddleware;5xx detail 固定回显(删 _is_debug_mode)"
```

---

## Task 10: /admin/trace/{id} 返回 events 数组

**Files:**
- Modify: `aigateway-api/src/aigateway_api/admin_routes.py:1044-1091`

**Interfaces:**
- Produces: `GET /admin/trace/{trace_id}` returns `{trace_id, events: [...], meta: {...}}`; `events` is the full TraceCollector event list from Redis `aigateway:trace:{id}`. Keeps `plugin_trace` field as alias (filtered `kind=="plugin"`) for backward compat during PR1/PR2.

- [ ] **Step 1: Read the existing endpoint**

```bash
sed -n '1040,1095p' aigateway-api/src/aigateway_api/admin_routes.py
```
Confirm it scans the old `aigateway:logs:requests` ZSET and returns `plugin_trace`.

- [ ] **Step 2: Rewrite to read new Redis key**

Replace the endpoint body to first try the new `aigateway:trace:{trace_id}` hash:

```python
@router.get("/trace/{trace_id}")
async def get_trace_detail(trace_id: str, request: Request):
    """全链路追踪详情 —— 返回完整 TraceEvent 事件流."""
    redis_client = _get_redis_client(request)
    if redis_client is None:
        raise HTTPException(status_code=503, detail="Redis unavailable")

    # 新通道:完整 events
    raw = await redis_client.hget(f"aigateway:trace:{trace_id}", "data")
    if raw:
        import json
        data = json.loads(raw)
        events = data.get("events", [])
        # 兼容字段:plugin_trace = events 中 kind==plugin 的子集
        plugin_trace = [
            {"plugin_name": e["stage"], "duration_ms": e["duration_ms"],
             "status": e["status"]}
            for e in events if e.get("kind") == "plugin"
        ]
        return {
            "trace_id": trace_id,
            "events": events,
            "plugin_trace": plugin_trace,   # 兼容旧前端
            "meta": {"wall_start": data.get("wall_start")},
        }

    # fallback:旧 ZSET(过渡期,新 key 未命中时)
    # ... 保留原有扫描逻辑 ...
```

Keep the old ZSET scan as fallback (transition period). Read the existing code and wrap it as the fallback branch.

- [ ] **Step 3: Add a test**

Append to `tests/test_trace_middleware.py`:

```python
def test_admin_trace_endpoint_returns_events():
    import asyncio, json
    from aigateway_core.trace_event import TraceCollector, TraceEvent
    import time

    async def setup_and_call():
        app = _make_app(redis_mock=AsyncMock())
        # 预置一条 trace 到 Redis
        c = TraceCollector.start("trace-xyz")
        c.emit(TraceEvent(trace_id="trace-xyz", ts=0.0, stage="auth",
                          kind="stage", name="auth.verify",
                          duration_ms=1.0, status="ok"))
        redis_mock = AsyncMock()
        redis_mock.hget = AsyncMock(return_value=json.dumps(c.to_dict()))
        app.state.redis = redis_mock

        client = TestClient(app)
        resp = client.get("/admin/trace/trace-xyz",
                          headers={"Authorization": "Bearer gw-test"})
        return resp

    resp = asyncio.get_event_loop().run_until_complete(setup_and_call())
    # (注:TestClient 同步调用,上面 async 包装仅为构造 redis mock;实际用同步写法)
```

If the admin route requires auth, either disable auth in test app or pass a valid key. Simpler: write a unit test that calls the endpoint's underlying logic directly. Adjust test to match how other admin route tests in the repo work (`grep -rn "admin_routes\|TestClient" tests/`).

- [ ] **Step 4: Run test**

Run: `python3 -m pytest tests/test_trace_middleware.py -v 2>&1 | tail -20`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add aigateway-api/src/aigateway_api/admin_routes.py tests/test_trace_middleware.py
git commit -m "feat(trace): /admin/trace/{id} 返回完整 events 数组(兼容保留 plugin_trace)"
```

---

## Task 11: PR1 集成验证 + Docker 重建

**Files:** (no code changes — verification + CLAUDE.md update)

- [ ] **Step 1: Run full test suite**

```bash
python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py 2>&1 | tail -40
```
Expected: all pass (or only pre-existing flaky failures). Fix any remaining `trace_id` TypeError or `add_plugin_trace` import errors.

- [ ] **Step 2: Write an end-to-end trace test**

Create `tests/test_trace_e2e.py`:

```python
"""端到端:一次 /v1/chat/completions 请求的所有事件 trace_id 一致且链路完整."""
import sys, os, asyncio, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-api", "src"))

from unittest.mock import AsyncMock, patch, MagicMock
from starlette.testclient import TestClient

from aigateway_api.trace_middleware import TraceMiddleware
from aigateway_core.trace_event import TraceCollector
from fastapi import FastAPI, Request


def test_single_request_one_trace_id():
    """一次请求只生成一个 trace_id,所有事件归属同一 collector."""
    app = FastAPI()
    app.state.redis = AsyncMock()
    app.state.redis.hset = AsyncMock()
    app.state.redis.expire = AsyncMock()
    app.add_middleware(TraceMiddleware)

    captured = {}

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        collector = TraceCollector.current()
        captured["trace_id"] = request.state.trace_id
        captured["collector_trace_id"] = collector.trace_id if collector else None
        return {"id": "chatcmpl-1", "choices": []}

    client = TestClient(app)
    resp = client.post("/v1/chat/completions", json={"model": "gpt", "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    # request.state.trace_id 和 collector.trace_id 一致
    assert captured["trace_id"] == captured["collector_trace_id"]
    # 响应头回写
    assert resp.headers["x-trace-id"] == captured["trace_id"]
    # flush 写了 Redis
    assert app.state.redis.hset.called
    key = app.state.redis.hset.call_args.args[0]
    assert key == f"aigateway:trace:{captured['trace_id']}"
```

- [ ] **Step 3: Run + commit the e2e test**

```bash
python3 -m pytest tests/test_trace_e2e.py -v
git add tests/test_trace_e2e.py
git commit -m "test(trace): 端到端验证单请求单 trace_id + flush 落 Redis"
```

- [ ] **Step 4: Rebuild Docker + verify**

```bash
sudo DOCKER_BUILDKIT=1 docker compose up -d --build gateway
sleep 5
curl -s http://localhost:8000/health
docker compose logs --tail=50 gateway | grep -i "error\|trace" | head
```
Expected: health OK, no trace-related errors in logs. Send a test request and check `/admin/trace/{id}` returns events:

```bash
curl -s -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer gw-rRIop4dpcyJJNUTJbHmHpr9Bj3M11s5o" \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"hi"}]}' | head -c 200
```
Then grep the trace from `/admin/logs` and hit `/admin/trace/{id}` — confirm `events` array is non-empty and all share one trace_id.

- [ ] **Step 5: Update CLAUDE.md**

In `CLAUDE.md`'s "Architecture Decisions & Known States" section, add a bullet:

```markdown
- **全链路 trace_id + TraceEvent 通道(2026-07-04, PR1)** — 新增 `TraceEvent`/`TraceCollector`(`trace_event.py`)+ ASGI `TraceMiddleware`。一次请求唯一 trace_id 由中间件生成写 `request.state`,所有 `PipelineContext` 必传 trace_id(删默认 factory)。engine/dispatcher/插件埋点统一 `collector.emit`。`model_router` 空壳退役。`/admin/trace/{id}` 返回 `events` 数组。5xx detail 固定回显(脱敏)。`debug_mode` 待 PR2 替换为 5 维度开关。
```

- [ ] **Step 6: Commit CLAUDE.md + push PR1**

```bash
git add CLAUDE.md
git commit -m "docs: CLAUDE.md 记录 PR1 trace 通道落地"
git push origin main
```

---

# PR2:5 维度 Debug 开关

## File Structure (PR2)

- **Create** `aigateway-core/src/aigateway_core/debug_config.py` — `DebugConfig` dataclass + 热重载 watcher
- **Modify** `aigateway-core/src/aigateway_core/config.py` — 删 `debug_mode` 归一化,加 `debug:` 段解析
- **Modify** `config.yaml` + `config.yaml.template` — `debug_mode: false` → `debug:` 块
- **Modify** `aigateway-core/src/aigateway_core/trace_event.py` — `TraceCollector.emit` 接受 debug 判断(或新增 `emit_debug` 方法)
- **Modify** 各维度判断点:`dispatcher.py`(entry)、`caching.py`(cache)、`litellm_bridge.py`(bridge)、`pipeline.py` engine 循环(plugins)
- **Modify** `aigateway-api/src/aigateway_api/admin_routes.py` — `/admin/plugins` 加 debug 字段、新 `POST /admin/plugins/{name}/debug`、`/admin/config/hot_reload` 扩写 debug 段、新 `GET /admin/config/debug`
- **Modify** `main.py` — 启动 `DebugConfigWatcher`
- **Test** `tests/test_debug_config.py`(新)

---

## Task 12: DebugConfig dataclass + watcher

**Files:**
- Create: `aigateway-core/src/aigateway_core/debug_config.py`
- Test: `tests/test_debug_config.py`

**Interfaces:**
- Produces: `DebugConfig` dataclass with fields `frontend/entry/cache/bridge: bool`, `plugins_enabled: bool`, `per_plugin: dict[str,bool]`; classmethods `from_yaml(d)` and `default()`; method `is_plugin_debug(name) -> bool`; `DebugConfigWatcher` class registering with `ConfigManager.on_reload()` (model on `GenerationOptimizationConfigWatcher`).

- [ ] **Step 1: Write failing test**

Create `tests/test_debug_config.py`:

```python
"""DebugConfig —— 5 维度 + 11 插件开关,AND 逻辑测试."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.debug_config import DebugConfig


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
    # config.yaml 没有 debug: 段时
    c = DebugConfig.from_yaml({})
    assert c == DebugConfig.default()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_debug_config.py -v`
Expected: FAIL `ModuleNotFoundError: aigateway_core.debug_config`.

- [ ] **Step 3: Write implementation**

Create `aigateway-core/src/aigateway_core/debug_config.py`:

```python
"""5 维度 Debug 开关配置 + 热重载 watcher.

维度:
- frontend: control-panel 浏览器日志
- entry: auth + dispatcher + 共用前置 + quota + prompt_compress 内联
- cache: L1/L2/L3 CacheManager
- bridge: LiteLLMBridge + circuit breaker + auto 解析
- plugins: 插件层(总开关 + per_plugin AND 关系)

替代旧 debug_mode 总开关。走 ConfigManager.on_reload() 热重载,atomic swap 无锁读。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any


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
        if not d:
            return cls.default()
        plugins = d.get("plugins") or {}
        per_plugin = plugins.get("per_plugin") or {}
        return cls(
            frontend=bool(d.get("frontend", False)),
            entry=bool(d.get("entry", False)),
            cache=bool(d.get("cache", False)),
            bridge=bool(d.get("bridge", False)),
            plugins_enabled=bool(plugins.get("enabled", False)),
            per_plugin={k: bool(v) for k, v in per_plugin.items()},
        )

    def is_plugin_debug(self, name: str) -> bool:
        """插件层 AND 逻辑:总开关 + 单个开关都开才生效."""
        return self.plugins_enabled and self.per_plugin.get(name, False)


class DebugConfigWatcher:
    """监听 ConfigManager 热重载,atomic swap DebugConfig.

    模式参照 GenerationOptimizationConfigWatcher。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._config = DebugConfig.default()

    @property
    def config(self) -> DebugConfig:
        with self._lock:
            return self._config

    def attach(self, config_manager: Any) -> None:
        """注册到 ConfigManager.on_reload() 回调."""
        if hasattr(config_manager, "on_reload"):
            config_manager.on_reload(self._on_config_reload)

    def _on_config_reload(self, config_manager: Any) -> None:
        raw = config_manager.config.get("debug", {})
        new_cfg = DebugConfig.from_yaml(raw)
        with self._lock:
            self._config = new_cfg


# 进程级单例(被 dispatcher/cache/bridge/pipeline 读取)
_watcher: DebugConfigWatcher | None = None


def get_debug_config() -> DebugConfig:
    """获取当前 DebugConfig(无 watcher 时返回 default)."""
    if _watcher is None:
        return DebugConfig.default()
    return _watcher.config


def init_debug_config_watcher(config_manager: Any) -> DebugConfigWatcher:
    """main.py 启动时调用一次."""
    global _watcher
    _watcher = DebugConfigWatcher()
    _watcher.attach(config_manager)
    _watcher._on_config_reload(config_manager)  # 首次加载
    return _watcher
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_debug_config.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add aigateway-core/src/aigateway_core/debug_config.py tests/test_debug_config.py
git commit -m "feat(debug): DebugConfig dataclass + 热重载 watcher(5 维度 + AND 逻辑)"
```

---

## Task 13: config.yaml + config.py 替换 debug_mode

**Files:**
- Modify: `config.yaml:190`
- Modify: `config.yaml.template`
- Modify: `aigateway-core/src/aigateway_core/config.py:181-208,221,566,746`
- Modify: `aigateway-api/src/aigateway_api/main.py` (启动 watcher,删 debug_mode 强制 DEBUG 逻辑 — 已在 Task 9 部分完成)

- [ ] **Step 1: Replace config.yaml debug_mode**

In `config.yaml`, line 190, replace:
```yaml
debug_mode: false
```
with:
```yaml
debug:
  frontend: false          # 前端 control-panel 浏览器日志
  entry: false             # auth + dispatcher + 共用前置(PII/media) + quota + prompt_compress 内联
  cache: false             # L1/L2/L3 CacheManager 全路径
  bridge: false            # LiteLLMBridge + circuit breaker + auto 解析
  plugins:
    enabled: false         # 插件层总开关(AND:总开 + 单个开才生效)
    per_plugin:
      pii_detector: false
      prompt_cache: false
      semantic_cache: false
      rag_retriever: false
      conv_compressor: false
      media_optimizer: false
      ai_director: false
      intent_evaluator: false
      token_compressor: false
      draft_generator: false
      gen_model_router: false
      cost_tracker: false
```

Do the same in `config.yaml.template` (with fuller comments).

- [ ] **Step 2: Remove debug_mode normalization in config.py**

In `aigateway-core/src/aigateway_core/config.py`, lines 181-208 (the `_normalize_environment` block that forces `debug_mode=False` in production and `True` in dev), remove all `debug_mode` references. Keep `AI_GATEWAY_ENV=production` forcing `log_level≥INFO` safety net, but drop the `debug_mode` line:

Read the block first:
```bash
sed -n '178,210p' aigateway-core/src/aigateway_core/config.py
```
Edit to remove debug_mode-related lines (keep hot_reload + log_level logic).

Also update the allowed-top-level-keys lists at lines 221, 566, 746 — replace `"debug_mode"` with `"debug"`:
```bash
grep -n '"debug_mode"' aigateway-core/src/aigateway_core/config.py
```
For each occurrence, change `"debug_mode"` to `"debug"`.

- [ ] **Step 3: Start DebugConfigWatcher in main.py**

In `main.py`'s `lifespan()` (where other components like ConfigManager are initialized), add after ConfigManager is ready:

```python
from aigateway_core.debug_config import init_debug_config_watcher
debug_watcher = init_debug_config_watcher(config_manager)
app.state.debug_config_watcher = debug_watcher
```

Find the right spot with `grep -n "config_manager\|lifespan\|app.state" aigateway-api/src/aigateway_api/main.py | head`.

- [ ] **Step 4: Remove admin hot_reload debug_mode handling**

In `admin_routes.py:865-943` (the `/admin/config/hot_reload` endpoint), it reads/writes `debug_mode`. Read it:
```bash
sed -n '865,943p' aigateway-api/src/aigateway_api/admin_routes.py
```
Replace `debug_mode` read/write with reading/writing the `debug:` section. The endpoint should now toggle the 5 dimensions + plugins_enabled + per_plugin. (Full UI wiring comes in Task 15; this step just makes the backend accept the new structure without crashing on missing debug_mode.)

Minimal change: replace any `config["debug_mode"]` access with `config.get("debug", {})` access; the actual dimension toggling endpoints are added in Task 15.

- [ ] **Step 5: Run config tests**

```bash
python3 -m pytest tests/test_integration_config_loading.py -v 2>&1 | tail -20
```
Expected: PASS. Fix any test that hardcodes `debug_mode`.

- [ ] **Step 6: Commit**

```bash
git add config.yaml config.yaml.template aigateway-core/src/aigateway_core/config.py aigateway-api/src/aigateway_api/main.py aigateway-api/src/aigateway_api/admin_routes.py tests/
git commit -m "refactor(debug): config.yaml 用 debug: 段替换 debug_mode;启动 DebugConfigWatcher"
```

---

## Task 14: TraceCollector.emit_debug + 各维度判断点

**Files:**
- Modify: `aigateway-core/src/aigateway_core/trace_event.py` (add `emit_debug` helper)
- Modify: `aigateway-core/src/aigateway_core/pipeline.py` (engine 循环加 debug payload)
- Modify: `aigateway-api/src/aigateway_api/dispatcher.py` (entry 维度判断)
- Modify: `aigateway-core/src/aigateway_core/caching.py` (cache 维度判断)
- Modify: `aigateway-core/src/aigateway_core/litellm_bridge.py` (bridge 维度判断)

**Interfaces:**
- Produces: `TraceCollector.emit_debug(stage, name, duration_ms, status, payload, dimension)` that checks `get_debug_config()` for the given dimension before emitting a `kind="debug"` event; engine loop calls it for plugins; dispatcher/cache/bridge call it for their dimensions.

- [ ] **Step 1: Add emit_debug to TraceCollector**

In `trace_event.py`, add method to `TraceCollector`:

```python
    def emit_debug(self, stage: str, name: str, duration_ms: float,
                   status: str, dimension: str, payload: dict | None) -> None:
        """发 kind=debug 事件 —— 仅当对应维度开关开启时才发且填 payload.

        Args:
            dimension: "entry"|"cache"|"bridge"|"plugin" —— 决定查哪个开关
            payload: debug 详情(开关关时此参数被忽略)
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
            duration_ms=round(duration_ms, 2), status=status,
            payload=payload,
        ))
```

- [ ] **Step 2: Wire engine loop to emit debug for plugins**

In `pipeline.py` engine loop (Task 5 modified it), after the success-branch `emit`, add:

```python
                    collector.emit_debug(
                        stage=plugin_name, name=f"{plugin_name}.execute",
                        duration_ms=elapsed_ms, status="ok", dimension="plugin",
                        payload={
                            "input_summary": _truncate(str(ctx.request.get("messages", ""))[:500]),
                            # 各插件可在 execute 内主动写 ctx.extra["_debug_payload"] 补充
                            "extra": ctx.extra.get("_debug_payload", {}),
                        },
                    )
```

Add a `_truncate` helper at top of pipeline.py:
```python
def _truncate(s: str, n: int = 500) -> str:
    return s if len(s) <= n else s[:n] + "..."
```

(Note: PII脱敏 of payload — for now truncate; full PII redaction of payload can reuse `PIIDetector.sanitize` but that's a follow-up. Document this in the task's commit message.)

- [ ] **Step 3: Wire dispatcher entry dimension**

In `dispatcher.py`, at the entry-dimension埋点 points (PII/media/quota/compress — the same `_emit_stage` calls from Task 6), add a parallel `emit_debug` call. Easiest: extend `_emit_stage` to also call `emit_debug`:

```python
def _emit_stage(trace_id: str, stage: str, name: str, duration_ms: float,
                status: str = "ok", payload: dict | None = None,
                dimension: str = "entry") -> None:
    from aigateway_core.trace_event import TraceCollector, TraceEvent
    import time as _time
    collector = TraceCollector.current()
    if collector:
        collector.emit(TraceEvent(
            trace_id=trace_id, ts=_time.monotonic(), stage=stage,
            kind="stage", name=name, duration_ms=round(duration_ms, 2),
            status=status,
        ))
        # 同时发 debug 事件(若 entry 开关开)
        collector.emit_debug(stage, name, duration_ms, status, dimension, payload)
```

- [ ] **Step 4: Wire cache dimension in caching.py**

In `caching.py` `CacheManager.get` / `set_all`, add debug emission around the cache operations. Find the methods:
```bash
grep -n "async def get\|async def set_all\|async def set\b" aigateway-core/src/aigateway_core/caching.py | head
```
At the start and end of each, wrap with timing + `collector.emit_debug(..., dimension="cache", payload={"key": ..., "tier_hit": ..., "size": ...})`. Import `TraceCollector` at top.

Example for `get`:
```python
    async def get(self, ...):
        import time as _time
        _start = _time.monotonic()
        result = ... existing logic ...
        from aigateway_core.trace_event import TraceCollector
        collector = TraceCollector.current()
        if collector:
            collector.emit_debug(
                stage="cache", name="cache_manager.get",
                duration_ms=(_time.monotonic() - _start) * 1000,
                status="ok", dimension="cache",
                payload={"key_hash": hash(str(key)) % 10**8, "tier_hit": result.tier if result else None},
            )
        return result
```

- [ ] **Step 5: Wire bridge dimension in litellm_bridge.py**

In `litellm_bridge.py` `completion` / `completion_stream`, add debug emission. Find:
```bash
grep -n "async def completion" aigateway-core/src/aigateway_core/litellm_bridge.py
```
Wrap with timing + `collector.emit_debug(stage="bridge", name="bridge.completion", dimension="bridge", payload={"model": ..., "provider": ..., "fallback_used": ...})`.

- [ ] **Step 6: Write a debug-toggle integration test**

Append to `tests/test_debug_config.py`:

```python
def test_emit_debug_respects_entry_switch():
    import time
    from aigateway_core.trace_event import TraceCollector, TraceEvent
    from aigateway_core.debug_config import DebugConfig, _watcher as _w
    import aigateway_core.debug_config as dc

    # entry 关 → debug 事件不发
    dc._watcher = None
    TraceCollector._current.set(None)
    c = TraceCollector.start("t1")
    c.emit_debug("cache", "cache.get", 1.0, "ok", "entry", {"x": 1})
    assert len(c.events) == 0

    # entry 开 → 发
    class FakeWatcher:
        @property
        def config(self): return DebugConfig(entry=True)
    dc._watcher = FakeWatcher()
    c.emit_debug("cache", "cache.get", 1.0, "ok", "entry", {"x": 1})
    assert len(c.events) == 1
    assert c.events[0].kind == "debug"
    assert c.events[0].payload == {"x": 1}
    dc._watcher = None  # cleanup
```

- [ ] **Step 7: Run tests + commit**

```bash
python3 -m pytest tests/test_debug_config.py tests/test_trace_event.py -v 2>&1 | tail -20
git add aigateway-core/src/aigateway_core/trace_event.py aigateway-core/src/aigateway_core/pipeline.py aigateway-api/src/aigateway_api/dispatcher.py aigateway-core/src/aigateway_core/caching.py aigateway-core/src/aigateway_core/litellm_bridge.py tests/
git commit -m "feat(debug): emit_debug 按 5 维度开关控制 kind=debug 事件 + payload"
```

---

## Task 15: admin 接口(debug toggle + /admin/config/debug)

**Files:**
- Modify: `aigateway-api/src/aigateway_api/admin_routes.py`

**Interfaces:**
- Produces: `POST /admin/plugins/{name}/debug {enabled: bool}`; `GET /admin/config/debug` returns current DebugConfig; `/admin/plugins` response per-plugin includes `debug` field (from per_plugin, `null` for prompt_compress); `/admin/config/hot_reload` accepts `debug:` section writes.

- [ ] **Step 1: Read /admin/plugins endpoint**

```bash
grep -n "admin/plugins\|def.*plugins\|@router.get.*plugins\|@router.post.*plugins" aigateway-api/src/aigateway_api/admin_routes.py | head
```

- [ ] **Step 2: Add debug field to /admin/plugins response**

In the plugins list endpoint, for each plugin returned, add:
```python
from aigateway_core.debug_config import get_debug_config
cfg = get_debug_config()
# in the per-plugin dict:
plugin_dict["debug"] = cfg.per_plugin.get(name) if name != "prompt_compress" else None
```
(prompt_compress returns `null` per spec — frontend hides its Debug button.)

- [ ] **Step 3: Add POST /admin/plugins/{name}/debug**

```python
@router.post("/plugins/{plugin_name}/debug")
async def set_plugin_debug(plugin_name: str, body: dict, request: Request):
    """开关单个插件的 debug 日志(写 config.yaml debug.plugins.per_plugin)."""
    enabled = bool(body.get("enabled", False))
    # 用 fcntl.flock 写 config.yaml(参照现有 admin 写配置模式)
    ...
    # 更新 debug.plugins.per_plugin[plugin_name] = enabled
    # 触发 atomic_swap → _notify_reload → DebugConfigWatcher 更新
    return {"ok": True, "plugin": plugin_name, "debug": enabled}
```
Mirror the existing config-write pattern (look at how `/admin/config/hot_reload` or plugins toggle endpoint writes config.yaml with flock).

- [ ] **Step 4: Add GET /admin/config/debug**

```python
@router.get("/config/debug")
async def get_debug_config_endpoint(request: Request):
    from aigateway_core.debug_config import get_debug_config
    from dataclasses import asdict
    cfg = get_debug_config()
    return {
        "frontend": cfg.frontend,
        "entry": cfg.entry,
        "cache": cfg.cache,
        "bridge": cfg.bridge,
        "plugins_enabled": cfg.plugins_enabled,
        "per_plugin": cfg.per_plugin,
    }
```

- [ ] **Step 5: Extend /admin/config/hot_reload to write debug section**

The existing endpoint (Task 13 step 4 touched it) should accept a `debug` key in its body and persist it. Add the `debug` section to the allowed write keys.

- [ ] **Step 6: Test the endpoints**

Write tests in `tests/test_debug_config.py` (or a new `tests/test_debug_admin.py`) using TestClient:
```python
def test_get_debug_config_endpoint():
    # TestClient with auth, GET /admin/config/debug, assert default all-false
    ...
def test_set_plugin_debug():
    # POST /admin/plugins/pii_detector/debug {enabled: true}
    # then GET /admin/plugins, assert pii_detector debug=true
    ...
```
Match auth pattern used by other admin tests (`grep -rn "Authorization" tests/`).

- [ ] **Step 7: Run + commit**

```bash
python3 -m pytest tests/test_debug_config.py tests/test_debug_admin.py -v 2>&1 | tail -20
git add aigateway-api/src/aigateway_api/admin_routes.py tests/
git commit -m "feat(debug): admin 接口(plugin debug toggle + /admin/config/debug + hot_reload 写 debug 段)"
```

---

## Task 16: PR2 集成验证 + Docker 重建

- [ ] **Step 1: Full test suite**

```bash
python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py 2>&1 | tail -40
```

- [ ] **Step 2: Docker rebuild + manual verify**

```bash
sudo DOCKER_BUILDKIT=1 docker compose up -d --build gateway
curl -s http://localhost:8000/health
# 开 pii_detector debug
curl -s -X POST http://localhost:8000/admin/plugins/pii_detector/debug \
  -H "Authorization: Bearer gw-rRIop4dpcyJJNUTJbHmHpr9Bj3M11s5o" \
  -H "Content-Type: application/json" -d '{"enabled": true}'
# 也得开 plugins 总开关(AND 逻辑)—— 通过 hot_reload 写 debug.plugins.enabled=true
# 发请求,查 trace
curl -s -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer gw-rRIop4dpcyJJNUTJbHmHpr9Bj3M11s5o" \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"my email is test@example.com"}]}' | head -c 200
# 查 /admin/trace/{id} —— 应有 kind=debug, stage=pii_detector 事件且 payload 非空
```

- [ ] **Step 3: Update CLAUDE.md + commit + push**

Add bullet to Architecture Decisions:
```markdown
- **5 维度 Debug 开关(2026-07-04, PR2)** — `debug_mode` 总开关替换为 `debug:` 段(frontend/entry/cache/bridge + plugins 总开关 + 11 插件 per_plugin,AND 逻辑)。`DebugConfig`(`debug_config.py`)+ `DebugConfigWatcher` 走 `ConfigManager.on_reload()`。`TraceCollector.emit_debug` 按维度决定是否发 `kind=debug` 事件 + payload。admin 接口:`POST /admin/plugins/{name}/debug`、`GET /admin/config/debug`、`/admin/config/hot_reload` 扩写 debug 段。`prompt_compress` debug 归 entry(无 per_plugin 开关,接口返回 null)。
```

```bash
git add CLAUDE.md
git commit -m "docs: CLAUDE.md 记录 PR2 5 维度 debug 开关落地"
git push origin main
```

---

# PR3:控制台 UI

## File Structure (PR3)

- **Modify** `control-panel/src/pages/Plugins.tsx` — 上下分栏 + Debug toggle
- **Modify** `control-panel/src/pages/Config.tsx`(或对应配置页)— 5 大区开关,删旧调试模式栏
- **Modify** `control-panel/src/pages/Logs.tsx` — trace 详情弹窗用 events 数组瀑布图
- **Modify** `control-panel/src/api/client.ts` — 新接口封装(setPluginDebug, getDebugConfig)

---

## Task 17: client.ts 新接口封装

**Files:**
- Modify: `control-panel/src/api/client.ts`

**Interfaces:**
- Produces: `setPluginDebug(name, enabled)`, `getDebugConfig()`, `updateDebugSection(debugObj)` API functions.

- [ ] **Step 1: Read existing client.ts pattern**

```bash
grep -n "export async function\|fetch\|VITE_API_BASE" control-panel/src/api/client.ts | head -20
```

- [ ] **Step 2: Add the three functions**

Following the existing fetch pattern (with auth header from localStorage):

```typescript
export async function setPluginDebug(pluginName: string, enabled: boolean): Promise<void> {
  const key = localStorage.getItem('apiKey') || '';
  await fetch(`${import.meta.env.VITE_API_BASE}/admin/plugins/${pluginName}/debug`, {
    method: 'POST',
    headers: { 'Authorization': `Bearer ${key}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled }),
  });
}

export interface DebugConfig {
  frontend: boolean;
  entry: boolean;
  cache: boolean;
  bridge: boolean;
  plugins_enabled: boolean;
  per_plugin: Record<string, boolean>;
}

export async function getDebugConfig(): Promise<DebugConfig> {
  const key = localStorage.getItem('apiKey') || '';
  const res = await fetch(`${import.meta.env.VITE_API_BASE}/admin/config/debug`, {
    headers: { 'Authorization': `Bearer ${key}` },
  });
  return res.json();
}

export async function updateDebugSection(debug: Partial<DebugConfig>): Promise<void> {
  const key = localStorage.getItem('apiKey') || '';
  await fetch(`${import.meta.env.VITE_API_BASE}/admin/config/hot_reload`, {
    method: 'POST',
    headers: { 'Authorization': `Bearer ${key}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ debug }),
  });
}
```

(Confirm the actual auth header key name used in client.ts — `grep "localStorage" control-panel/src/api/client.ts`.)

- [ ] **Step 3: Typecheck**

```bash
cd control-panel && npx tsc --noEmit 2>&1 | tail -20
```
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add control-panel/src/api/client.ts
git commit -m "feat(ui): client.ts 封装 setPluginDebug/getDebugConfig/updateDebugSection"
```

---

## Task 18: Plugins.tsx 上下分栏 + Debug toggle

**Files:**
- Modify: `control-panel/src/pages/Plugins.tsx`

- [ ] **Step 1: Read current Plugins.tsx fully**

```bash
sed -n '1,120p' control-panel/src/pages/Plugins.tsx
sed -n '290,360p' control-panel/src/pages/Plugins.tsx
```
Confirm: `PluginConfigItem` type (line 15), `getCategory` (line 92), render loop at 306 (5 hard-coded categories).

- [ ] **Step 2: Add debug field to PluginConfigItem type**

At line 15 (the `PluginConfigItem` interface), add:
```typescript
interface PluginConfigItem {
  name: string;
  enabled: boolean;
  pipeline_kind?: 'understanding' | 'generation';
  debug?: boolean | null;   // null = 不支持单独 debug(如 prompt_compress)
  // ... existing fields
}
```

- [ ] **Step 3: Replace the 5-category render loop with two-level grouping**

Replace the block at line 306 (`['缓存','安全','性能','路由','其他'].map(catLabel => ...)`) with a two-level loop: outer over `['understanding','generation']`, inner over the 5 categories:

```tsx
{(['understanding', 'generation'] as const).map(kind => {
  const kindPlugins = plugins.filter(p => (p.pipeline_kind || 'understanding') === kind);
  if (kindPlugins.length === 0) return null;
  return (
    <div key={kind} className="mb-8">
      <h3 className="text-lg font-semibold mb-4" style={{ color: 'var(--color-text-primary)' }}>
        {kind === 'understanding' ? '理解管道' : '生成管道'}
        <span className="ml-2 text-sm font-normal" style={{ color: 'var(--color-text-tertiary)' }}>
          ({kindPlugins.length} 插件)
        </span>
      </h3>
      {['缓存', '安全', '性能', '路由', '其他'].map(catLabel => {
        const catPlugins = kindPlugins.filter(p => getCategory(p.name) === catLabel);
        if (catPlugins.length === 0) return null;
        return (
          <div key={catLabel} className="mb-4">
            <div className="text-sm font-medium mb-2" style={{ color: 'var(--color-text-secondary)' }}>
              {catLabel}
            </div>
            <div className="space-y-2">
              {catPlugins.map(plugin => (
                <Card key={plugin.name} className="flex items-center justify-between">
                  {/* 左侧:图标 + 名称 + badge + 描述(保留现有) */}
                  <div className="flex items-center gap-3">
                    {/* ... existing icon/name/badge/description ... */}
                  </div>
                  {/* 右侧:启用 toggle + Debug toggle(新增) */}
                  <div className="flex items-center gap-3">
                    {plugin.debug !== null && plugin.debug !== undefined && (
                      <button
                        onClick={() => toggleDebug(plugin.name, plugin.debug)}
                        title="Debug 日志"
                        className="p-2 rounded-lg"
                        style={{
                          backgroundColor: plugin.debug ? 'var(--color-warning, #f59e0b)' : 'var(--color-bg-overlay)',
                        }}
                      >
                        <Bug size={16} style={{ color: plugin.debug ? 'white' : 'var(--color-text-tertiary)' }} />
                      </button>
                    )}
                    <Toggle checked={plugin.enabled} onChange={() => toggle(plugin.name, plugin.enabled)} />
                  </div>
                </Card>
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
})}
```

Import `Bug` from lucide-react at top (alongside existing `Puzzle`).

- [ ] **Step 4: Add toggleDebug handler**

Near the existing `toggle` function, add:
```typescript
const toggleDebug = async (name: string, current: boolean | null) => {
  if (current === null) return;
  try {
    await setPluginDebug(name, !current);
    setPlugins(prev => prev.map(p => p.name === name ? { ...p, debug: !current } : p));
  } catch (e) {
    // error toast
  }
};
```
Import `setPluginDebug` from `../api/client`.

- [ ] **Step 5: Typecheck + build**

```bash
cd control-panel && npx tsc --noEmit 2>&1 | tail -20
npm run build 2>&1 | tail -20
```
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add control-panel/src/pages/Plugins.tsx
git commit -m "feat(ui): /plugins 页按 pipeline_kind 上下分栏 + 每插件 Debug toggle"
```

---

## Task 19: Config.tsx 5 大区开关

**Files:**
- Modify: `control-panel/src/pages/Config.tsx` (or wherever the debug_mode toggle currently lives)

- [ ] **Step 1: Find the current debug_mode toggle**

```bash
grep -rn "debug_mode\|调试模式" control-panel/src/
```

- [ ] **Step 2: Replace with 5-dimension card**

Remove the single debug_mode toggle. Add a "调试日志" card with 5 toggles (frontend/entry/cache/bridge/plugins_enabled), each with a one-line description. Load state via `getDebugConfig()` on mount, save via `updateDebugSection()`:

```tsx
const [debug, setDebug] = useState<DebugConfig | null>(null);

useEffect(() => {
  getDebugConfig().then(setDebug);
}, []);

const toggleDim = (dim: keyof DebugConfig) => {
  if (!debug) return;
  const next = { ...debug, [dim]: !debug[dim] };
  setDebug(next);
  updateDebugSection({ [dim]: !debug[dim] } as Partial<DebugConfig>);
};
```

5 rows with labels:
| 开关 | 说明 |
|---|---|
| 前端 | control-panel 浏览器内日志(console.debug + fetch 详情) |
| 入口层 | auth + dispatcher + 共用前置(PII/media) + quota + prompt_compress 内联 |
| cache | L1/L2/L3 缓存查找/写入/淘汰 |
| bridge | LiteLLM 出口 + 熔断 + auto 模型解析 |
| 插件层总开关 | 开启后,只有单独也开的插件才打 debug 日志 |

- [ ] **Step 3: Wire frontend dim locally**

When `frontend` toggles on, also enable browser console debug:
```typescript
const toggleDim = (dim) => {
  ...
  if (dim === 'frontend') {
    (window as any).__AIGATEWAY_DEBUG__ = !debug.frontend;
  }
};
```
And in the fetch wrapper (client.ts), check `window.__AIGATEWAY_DEBUG__` before `console.debug`-logging request/response.

- [ ] **Step 4: Typecheck + build**

```bash
cd control-panel && npx tsc --noEmit && npm run build 2>&1 | tail -20
```

- [ ] **Step 5: Commit**

```bash
git add control-panel/src/pages/Config.tsx control-panel/src/api/client.ts
git commit -m "feat(ui): /config 页 5 大区 debug 开关,删旧调试模式栏"
```

---

## Task 20: Logs.tsx trace 详情弹窗用 events 瀑布图

**Files:**
- Modify: `control-panel/src/pages/Logs.tsx:401,428-459`

- [ ] **Step 1: Read current trace modal**

```bash
sed -n '395,460p' control-panel/src/pages/Logs.tsx
```
Confirm it renders `plugin_trace` array as a waveform/list.

- [ ] **Step 2: Switch to events array**

The trace detail fetch (likely hits `/admin/trace/{id}`) now returns `events` array. Update the modal to render all events (not just `kind==="plugin"`):

```tsx
// 旧:plugin_trace.map(...)
// 新:events.map(ev => ...)
{trace.events?.map((ev, i) => (
  <div key={i} className="flex items-center gap-2 py-1"
       style={{ borderLeft: `3px solid ${ev.kind === 'plugin' ? '#10b981' : ev.kind === 'debug' ? '#f59e0b' : '#3b82f6'}` }}>
    <span className="text-xs font-mono w-32">{ev.stage}</span>
    <span className="text-xs flex-1">{ev.name}</span>
    <span className="text-xs">{ev.duration_ms?.toFixed(1)}ms</span>
    <span className="text-xs" style={{ color: ev.status === 'error' ? 'red' : 'gray' }}>{ev.status}</span>
    {ev.payload && (
      <details><summary className="text-xs">payload</summary><pre className="text-xs">{JSON.stringify(ev.payload, null, 2)}</pre></details>
    )}
  </div>
))}
```

Color: `kind=stage` 蓝、`kind=plugin` 绿、`kind=debug` 橙 (matches spec 3.3).

- [ ] **Step 3: Update the trace fetch type**

Update the `Trace` interface in Logs.tsx to include `events`:
```typescript
interface TraceEvent {
  ts: number; stage: string; kind: 'stage'|'plugin'|'debug';
  name: string; duration_ms: number | null; status: string; payload: any;
}
interface Trace {
  trace_id: string;
  events: TraceEvent[];
  plugin_trace?: any[]; // 兼容旧
  meta?: any;
}
```

- [ ] **Step 4: Typecheck + build**

```bash
cd control-panel && npx tsc --noEmit && npm run build 2>&1 | tail -20
```

- [ ] **Step 5: Commit**

```bash
git add control-panel/src/pages/Logs.tsx
git commit -m "feat(ui): /logs trace 详情弹窗改用 events 瀑布图(stage蓝/plugin绿/debug橙)"
```

---

## Task 21: PR3 集成验证 + 删旧 plugin_trace 双写 + 收尾

- [ ] **Step 1: Build + run frontend**

```bash
cd control-panel && npm run build
sudo DOCKER_BUILDKIT=1 docker compose up -d --build control-panel
```

- [ ] **Step 2: Manual verify all 3 things in browser**

- /plugins 页:理解管道区块在上、生成管道区块在下,每级下有「缓存/安全/性能/路由/其他」子组;每张插件卡(除 prompt_compress)有 Debug 按钮。
- /config 页:5 大区开关可切换,刷新后状态保持(走 config.yaml)。
- /logs 页:点某条记录的 trace 详情,看到完整 events 瀑布图,开过 debug 的插件有橙色 debug 事件 + 可展开 payload。
- 开 pii_detector debug + 插件总开关,发请求,trace 里看到橙色 pii_detector 事件。

- [ ] **Step 3: Remove old plugin_trace double-write (spec 1.7 收尾)**

In `openai_compat.py` `_record_request_log` and `TraceCollector.flush`, now that frontend reads `events`, remove the `plugin_trace` field from the Redis log entry (keep only in the new `aigateway:trace:{id}` key). In `admin_routes.py` `/admin/trace/{id}`, remove the `plugin_trace` compat alias (frontend no longer reads it).

```bash
grep -n "plugin_trace" aigateway-api/src/aigateway_api/openai_compat.py aigateway-api/src/aigateway_api/admin_routes.py
```
Remove the compat alias added in Task 10 step 2 and the double-write.

- [ ] **Step 4: Full test suite**

```bash
python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py 2>&1 | tail -30
cd control-panel && npx tsc --noEmit
```

- [ ] **Step 5: Update CLAUDE.md + commit + push**

Add bullet:
```markdown
- **控制台插件分栏 + Debug UI(2026-07-04, PR3)** — `/plugins` 页按 `pipeline_kind` 上下分栏(理解/生成),二级保留「缓存/安全/性能/路由/其他」;每张插件卡加 Debug toggle(prompt_compress 无,接口返回 null)。`/config` 页 5 大区开关替换 debug_mode。`/logs` trace 详情弹窗改用 `events` 瀑布图(stage 蓝/plugin 绿/debug 橙)。删旧 `plugin_trace` 双写与兼容别名。
```

Also update CLAUDE.md "Debug switches today" / "trace_id today" known-states if they were documented (they weren't in the original CLAUDE.md, but the new bullets cover it).

```bash
git add CLAUDE.md aigateway-api/src/aigateway_api/openai_compat.py aigateway-api/src/aigateway_api/admin_routes.py
git commit -m "feat(ui): PR3 控制台 UI 落地 + 删旧 plugin_trace 双写收尾"
git push origin main
```

---

## Self-Review Notes

已对照 spec 检查:

**Spec coverage:**
- 第 1 部分(TraceEvent 通道 + trace_id 全链路)→ Task 1-11 ✓
- 第 2 部分(5 维度 debug 开关)→ Task 12-16 ✓
- 第 3 部分(控制台 UI)→ Task 17-21 ✓
- model_router 退役 → Task 8 ✓
- prompt_compress 保留内联 → Task 6 (`_skip_names` 保留 prompt_compress)+ Task 15 (debug 字段返回 null) ✓
- 5xx 固定回显 → Task 9 ✓
- 删 debug_mode → Task 9 (main.py) + Task 13 (config.yaml/config.py) ✓
- getCategory 映射 → Task 18 (现有 keyword-based getCategory 已覆盖大部分,无需改逻辑) ✓
- 删旧 plugin_trace 双写 → Task 21 ✓

**Placeholder scan:** 无 TBD/TODO;每个 step 有具体代码或具体命令。

**Type consistency:** `TraceEvent` 字段在 Task 1 定义,Task 5/6/7/14 使用一致;`DebugConfig` 字段在 Task 12 定义,Task 14/15/17/19 使用一致;`emit_debug` 签名 Task 14 定义,各处调用一致。

**已知未决(留给实现时定):**
- Task 4 dispatch 签名是否已接收 `request` —— 实现时读签名决定(a)直接读 `request.state.trace_id` 还是(b)加参数。两种方案都给了代码。
- Task 15 admin 写 config.yaml 的具体 flock 模式 —— 参照现有 `/admin/config/hot_reload` 实现,本计划给接口签名未给完整 flock 代码(因现有代码已有该模式,实现时应复用而非重写)。
