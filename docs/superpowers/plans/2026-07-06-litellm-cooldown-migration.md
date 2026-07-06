# CircuitBreaker → litellm cooldown 迁移 实施 Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除自实现的 CircuitBreaker/CircuitBreakerFactory,启用 litellm Router 内置 cooldown 熔断,让熔断与 fallback 由 litellm 统一调度;`/metrics` 与 admin 从 bridge 内维护的 tracker 读取真实状态。

**Architecture:** 在 LiteLLMBridge 内新增 `ProviderCooldownTracker`(内部类,不单独开文件),通过 litellm Router 的 `deployment_callback_on_failure` / `deployment_callback_on_success` 接收失败/成功事件维护 per-model 状态,再按 provider 聚合供 `/metrics` 同步读。config.yaml 的 `circuit_breaker:` 段字段(failure_threshold / recovery_timeout)映射到 litellm 的 `allowed_fails` / `cooldown_time`,向前兼容,用户无需改配置。

**Tech Stack:** Python 3.12 / litellm 1.83.7 / FastAPI / pytest / structlog

## Global Constraints

- litellm 版本:1.83.7(requirements.txt 已装)。Router 参数以此版本签名为准。
- config.yaml 用户零改动:`circuit_breaker:` 段字段名保留,内部映射到 litellm 参数。
- Prometheus 指标名保留:`set_circuit_breaker_state(provider, state)`,状态值 0=CLOSED / 1=OPEN(不用 2=HALF_OPEN,但保留数值语义占位)。
- Admin API 响应字段名 `circuit_breakers` 保留,前端不改。
- 代码内置默认值:allowed_fails=5、cooldown_time=60、long_open_alert_seconds=300。
- 提交规范:每完成一个 Task 单独 commit,conventional commit(feat/fix/refactor/test/docs/chore)。
- 每次代码改动后 Docker 重建 gateway 并 `/health` 验证(workflow rule 2)。
- CLAUDE.md 与 ARCHITECTURE_DIAGRAM.md 在 Task 9 统一同步。

---

## File Structure

**Delete:**
- `aigateway-core/src/aigateway_core/circuit_breaker.py`(439 行,整个文件)

**Modify:**
- `aigateway-core/src/aigateway_core/litellm_bridge.py`:
  - 新增 `ProviderCooldownTracker` 类(文件顶部)
  - `LiteLLMBridge.__init__` 加 `_cooldown_tracker` 字段
  - `LiteLLMBridge.initialize()` 传 `allowed_fails`/`cooldown_time` 给 Router,注册 callback
  - 新增 `get_cooldown_status()` / `get_cooldown_status_by_provider()` 方法
  - 移除 `completion()` 里的 `fallback_chain` 死代码循环(可选,Task 7)
- `aigateway-api/src/aigateway_api/main.py`:
  - 删除 `from aigateway_core.circuit_breaker import CircuitBreakerFactory`
  - 删除 `CircuitBreakerFactory` 初始化块
  - 删除 `app.state.circuit_breaker_factory = cb_factory`
- `aigateway-api/src/aigateway_api/routes.py`:
  - `/metrics`(line 45-60):从 `bridge.get_cooldown_status_by_provider()` 读
  - `/health`(line 143-146):从 `bridge.get_cooldown_status()` 读
  - 移除 `circuit_breaker_factory = getattr(...)` 引用
- `aigateway-api/src/aigateway_api/admin_routes.py`:
  - line 486 + 533-536:从 bridge 读,不再遍历 `_breakers`

**Create:**
- `tests/test_provider_cooldown_tracker.py`:单元测试 tracker 行为(6 个测试用例)

**Doc updates(Task 9 统一处理):**
- `CLAUDE.md`:Architecture Decisions 更新条目、Architecture at a Glance 表移除 circuit_breaker.py 行
- `docs/ARCHITECTURE_DIAGRAM.md`:C.1 图注改 "CircuitBreaker 包裹" → "litellm 内置 cooldown 熔断"

---

### Task 1: 为 ProviderCooldownTracker 写单元测试(TDD 第一步,tracker 尚未存在)

**Files:**
- Create: `tests/test_provider_cooldown_tracker.py`

**Interfaces:**
- Produces: 定义 `ProviderCooldownTracker` 的公开 API 契约(供 Task 2 实现):
  - `ProviderCooldownTracker(allowed_fails: int, cooldown_time: int, long_open_alert_seconds: int = 300)`
  - `on_failure(model: str) -> None` — 记一次失败,达阈值转 OPEN
  - `on_success(model: str) -> None` — 记一次成功,OPEN 转 CLOSED
  - `get_all_status() -> Dict[str, Dict[str, Any]]` — 返回 `{model_name: status_dict}`
  - `get_provider_states() -> Dict[str, int]` — 返回 `{provider: 0|1}`,任一 model OPEN → provider OPEN
  - status_dict 结构:`{state: "CLOSED"|"OPEN", state_value: 0|1, failure_count: int, last_failure_time: float, last_success_time: float, cooldown_until: float|None}`
  - `_extract_provider(model: str) -> str` — 从 model 名提取 provider(如 "openai/gpt-4o" → "openai";无 "/" 则返回原名)

- [ ] **Step 1: 写测试文件**

创建 `tests/test_provider_cooldown_tracker.py`:

```python
"""ProviderCooldownTracker 单元测试。

测 tracker 的状态转换、per-model 独立性、provider 级聚合。
不依赖 litellm/网络/Redis。
"""

import time
import pytest

from aigateway_core.litellm_bridge import ProviderCooldownTracker


def test_closed_by_default():
    """新建 tracker,任意 model 查询状态默认 CLOSED(未记录)。"""
    tracker = ProviderCooldownTracker(allowed_fails=5, cooldown_time=60)
    assert tracker.get_all_status() == {}
    # provider 级也应为空
    assert tracker.get_provider_states() == {}


def test_failure_below_threshold_stays_closed():
    """失败次数低于阈值,状态仍是 CLOSED。"""
    tracker = ProviderCooldownTracker(allowed_fails=3, cooldown_time=60)
    tracker.on_failure("openai/gpt-4o")
    tracker.on_failure("openai/gpt-4o")
    status = tracker.get_all_status()["openai/gpt-4o"]
    assert status["state"] == "CLOSED"
    assert status["state_value"] == 0
    assert status["failure_count"] == 2


def test_failure_at_threshold_transitions_to_open():
    """连续失败达阈值,状态转 OPEN,cooldown_until 被设置。"""
    tracker = ProviderCooldownTracker(allowed_fails=3, cooldown_time=60)
    for _ in range(3):
        tracker.on_failure("openai/gpt-4o")
    status = tracker.get_all_status()["openai/gpt-4o"]
    assert status["state"] == "OPEN"
    assert status["state_value"] == 1
    assert status["cooldown_until"] is not None
    assert status["cooldown_until"] > time.time()  # cooldown 尚未过期


def test_success_resets_failure_count_in_closed():
    """CLOSED 状态下的 success 重置 failure_count。"""
    tracker = ProviderCooldownTracker(allowed_fails=5, cooldown_time=60)
    tracker.on_failure("openai/gpt-4o")
    tracker.on_failure("openai/gpt-4o")
    tracker.on_success("openai/gpt-4o")
    status = tracker.get_all_status()["openai/gpt-4o"]
    assert status["failure_count"] == 0
    assert status["state"] == "CLOSED"


def test_success_recovers_from_open():
    """OPEN 状态下的 success 转回 CLOSED,清空 failure_count 和 cooldown_until。"""
    tracker = ProviderCooldownTracker(allowed_fails=2, cooldown_time=60)
    tracker.on_failure("anthropic/claude-3")
    tracker.on_failure("anthropic/claude-3")
    assert tracker.get_all_status()["anthropic/claude-3"]["state"] == "OPEN"
    tracker.on_success("anthropic/claude-3")
    status = tracker.get_all_status()["anthropic/claude-3"]
    assert status["state"] == "CLOSED"
    assert status["failure_count"] == 0
    assert status["cooldown_until"] is None


def test_per_model_independent():
    """多个 model 状态互相独立。"""
    tracker = ProviderCooldownTracker(allowed_fails=2, cooldown_time=60)
    tracker.on_failure("openai/gpt-4o")
    tracker.on_failure("openai/gpt-4o")  # openai OPEN
    tracker.on_failure("anthropic/claude-3")  # anthropic 1 次失败,仍 CLOSED
    all_status = tracker.get_all_status()
    assert all_status["openai/gpt-4o"]["state"] == "OPEN"
    assert all_status["anthropic/claude-3"]["state"] == "CLOSED"
    assert all_status["anthropic/claude-3"]["failure_count"] == 1


def test_provider_states_aggregate_worst():
    """provider 级聚合:同 provider 任一 model OPEN → provider OPEN。"""
    tracker = ProviderCooldownTracker(allowed_fails=1, cooldown_time=60)
    # openai 有两个 model,一个 OPEN 一个 CLOSED
    tracker.on_failure("openai/gpt-4o")  # OPEN(allowed_fails=1)
    tracker.on_success("openai/gpt-3.5")  # CLOSED
    states = tracker.get_provider_states()
    assert states["openai"] == 1  # 聚合为 OPEN
```

- [ ] **Step 2: 运行测试确认失败(tracker 尚未实现)**

命令:
```bash
python3 -m pytest tests/test_provider_cooldown_tracker.py -v 2>&1 | tail -15
```

预期:所有测试 FAIL 或 ERROR,原因是 `ImportError: cannot import name 'ProviderCooldownTracker' from 'aigateway_core.litellm_bridge'`。

- [ ] **Step 3: 提交测试(测试驱动锚点)**

```bash
git add tests/test_provider_cooldown_tracker.py
git commit -m "test(litellm-cooldown): 加 ProviderCooldownTracker 单元测试(7 用例,尚未实现)"
```

---

### Task 2: 实现 ProviderCooldownTracker

**Files:**
- Modify: `aigateway-core/src/aigateway_core/litellm_bridge.py`(在文件顶部 imports 后、`LiteLLMBridge` 类定义前插入)

**Interfaces:**
- Consumes: 无(纯 Python)
- Produces: `ProviderCooldownTracker` 类,签名见 Task 1 契约

- [ ] **Step 1: 在 litellm_bridge.py 顶部找到合适插入位置**

打开 `aigateway-core/src/aigateway_core/litellm_bridge.py`,定位第一个 `class LiteLLMBridge` 定义处(约 line 40-50 区),在其**前面**插入 tracker 类。

- [ ] **Step 2: 插入 ProviderCooldownTracker 实现**

在 `class LiteLLMBridge` 定义**前**插入:

```python
class ProviderCooldownTracker:
    """per-model cooldown 状态跟踪器。

    由 LiteLLMBridge 通过 litellm Router 的 deployment callback 驱动。
    litellm 内部自己也有 cooldown(_filter_cooldown_deployments),这里
    维护一份镜像供 /metrics 与 admin 同步读取(避免每次 /metrics 请求
    调 litellm async API)。

    状态:CLOSED(0)/ OPEN(1),不实现 HALF-OPEN(litellm 无对应概念)。
    """

    def __init__(
        self,
        allowed_fails: int = 5,
        cooldown_time: int = 60,
        long_open_alert_seconds: int = 300,
    ) -> None:
        self.allowed_fails = allowed_fails
        self.cooldown_time = cooldown_time
        self.long_open_alert_seconds = long_open_alert_seconds
        # {model_name: {"state": "CLOSED"/"OPEN", "failure_count": int,
        #               "last_failure_time": float, "last_success_time": float,
        #               "cooldown_until": float|None}}
        self._models: Dict[str, Dict[str, Any]] = {}
        import threading
        self._lock = threading.Lock()

    @staticmethod
    def _extract_provider(model: str) -> str:
        """从 model 名提取 provider,如 openai/gpt-4o → openai。"""
        if "/" in model:
            return model.split("/", 1)[0]
        return model

    def _get_or_init(self, model: str) -> Dict[str, Any]:
        if model not in self._models:
            self._models[model] = {
                "state": "CLOSED",
                "failure_count": 0,
                "last_failure_time": 0.0,
                "last_success_time": 0.0,
                "cooldown_until": None,
            }
        return self._models[model]

    def on_failure(self, model: str) -> None:
        """记一次失败;累计达 allowed_fails → 转 OPEN。"""
        if not model:
            return
        import time as _t
        with self._lock:
            entry = self._get_or_init(model)
            entry["failure_count"] += 1
            entry["last_failure_time"] = _t.time()
            if entry["state"] == "CLOSED" and entry["failure_count"] >= self.allowed_fails:
                entry["state"] = "OPEN"
                entry["cooldown_until"] = _t.time() + self.cooldown_time
                logger.warning(
                    "cooldown: model=%s → OPEN(连续失败 %d 次,cooldown %ds)",
                    model, entry["failure_count"], self.cooldown_time,
                )
            # long_open 告警(本次失败发生时如果已 OPEN 且时间过长,输出一次 error)
            if entry["state"] == "OPEN" and entry["cooldown_until"]:
                open_duration = _t.time() - (entry["cooldown_until"] - self.cooldown_time)
                if open_duration >= self.long_open_alert_seconds:
                    logger.error(
                        "cooldown alert: model=%s OPEN 持续 %.0fs 超过阈值 %ds",
                        model, open_duration, self.long_open_alert_seconds,
                    )

    def on_success(self, model: str) -> None:
        """记一次成功;OPEN → CLOSED,或 CLOSED 状态下重置 failure_count。"""
        if not model:
            return
        import time as _t
        with self._lock:
            entry = self._get_or_init(model)
            entry["last_success_time"] = _t.time()
            if entry["state"] == "OPEN":
                logger.info("cooldown: model=%s → CLOSED(恢复正常)", model)
            entry["state"] = "CLOSED"
            entry["failure_count"] = 0
            entry["cooldown_until"] = None

    def get_all_status(self) -> Dict[str, Dict[str, Any]]:
        """返回所有 model 状态的浅拷贝(供 /admin/health 读)。"""
        with self._lock:
            return {
                m: {
                    "state": e["state"],
                    "state_value": 0 if e["state"] == "CLOSED" else 1,
                    "failure_count": e["failure_count"],
                    "last_failure_time": e["last_failure_time"],
                    "last_success_time": e["last_success_time"],
                    "cooldown_until": e["cooldown_until"],
                }
                for m, e in self._models.items()
            }

    def get_provider_states(self) -> Dict[str, int]:
        """按 provider 聚合状态,任一 model OPEN → provider OPEN。

        供 /metrics 上报 Prometheus circuit_breaker_state gauge。
        """
        with self._lock:
            provider_state: Dict[str, int] = {}
            for m, e in self._models.items():
                p = self._extract_provider(m)
                cur = provider_state.get(p, 0)
                v = 0 if e["state"] == "CLOSED" else 1
                if v > cur:
                    provider_state[p] = v
                elif p not in provider_state:
                    provider_state[p] = v
            return provider_state
```

**注意**:上面代码用到 `Dict`/`Any`,确保 litellm_bridge.py 顶部 `from typing import ...` 已含它们(litellm_bridge.py 现已 import `Dict, Any`,无需新增)。`logger` 也用现有的模块级 logger。

- [ ] **Step 3: 运行 tracker 单元测试确认全绿**

命令:
```bash
python3 -m pytest tests/test_provider_cooldown_tracker.py -v 2>&1 | tail -15
```

预期:7 个测试全部 PASSED。

- [ ] **Step 4: 提交**

```bash
git add aigateway-core/src/aigateway_core/litellm_bridge.py
git commit -m "feat(litellm-bridge): 加 ProviderCooldownTracker(镜像 litellm cooldown 状态供同步读)"
```

---

### Task 3: LiteLLMBridge 初始化 tracker + 注册 litellm callback

**Files:**
- Modify: `aigateway-core/src/aigateway_core/litellm_bridge.py`(class `LiteLLMBridge`)

**Interfaces:**
- Consumes: `ProviderCooldownTracker`(Task 2 定义)
- Produces:
  - `LiteLLMBridge._cooldown_tracker: ProviderCooldownTracker` 实例字段
  - `LiteLLMBridge.get_cooldown_status() -> Dict[str, Dict[str, Any]]`
  - `LiteLLMBridge.get_cooldown_status_by_provider() -> Dict[str, int]`
  - litellm Router 已注入 `allowed_fails`、`cooldown_time`、`deployment_callback_on_failure`、`deployment_callback_on_success`

- [ ] **Step 1: `__init__` 中加 tracker 字段**

在 `LiteLLMBridge.__init__` 的 `self._auto_resolver: Any = None` 之后(约 litellm_bridge.py:69)加:

```python
        # cooldown tracker(初始化在 initialize() 里创建,init 时先占位)
        self._cooldown_tracker: Optional["ProviderCooldownTracker"] = None
```

**注意**:确保 `from typing import Optional` 已 import(litellm_bridge.py 现应已 import)。

- [ ] **Step 2: `initialize()` 里读 config、创建 tracker、传 Router 参数、注册 callback**

定位 `initialize()` 方法中 `self.router = Router(...)` 那一段(约 line 176-185),改为:

```python
            # 读取 circuit_breaker 段(向前兼容),映射到 litellm cooldown 参数
            cb_cfg = self.config.get("circuit_breaker", {}) if isinstance(self.config, dict) else {}
            allowed_fails = int(cb_cfg.get("failure_threshold", 5)) if cb_cfg else 5
            cooldown_time = int(cb_cfg.get("recovery_timeout", 60)) if cb_cfg else 60
            long_open_alert = int(cb_cfg.get("long_open_alert_seconds", 300)) if cb_cfg else 300

            # 创建 tracker(供 /metrics 与 /admin/health 同步读)
            self._cooldown_tracker = ProviderCooldownTracker(
                allowed_fails=allowed_fails,
                cooldown_time=cooldown_time,
                long_open_alert_seconds=long_open_alert,
            )

            self.router = Router(
                model_list=model_list,
                routing_strategy=routing_strategy_config,
                num_retries=getattr(self.config, "num_retries", 3)
                if hasattr(self.config, "num_retries")
                else 3,
                allowed_fails=allowed_fails,
                cooldown_time=cooldown_time,
            )

            # 注册 litellm deployment callback,把失败/成功事件转发给 tracker
            # litellm 1.83.7 callback 签名:async def(kwargs, response, start_time, end_time)
            async def _on_failure(kwargs, response, start_time, end_time):
                try:
                    model = kwargs.get("model", "") or (kwargs.get("litellm_params") or {}).get("model", "")
                    if self._cooldown_tracker:
                        self._cooldown_tracker.on_failure(model)
                except Exception as exc:
                    logger.warning("cooldown tracker on_failure 异常: %s", exc)

            async def _on_success(kwargs, response, start_time, end_time):
                try:
                    model = kwargs.get("model", "") or (kwargs.get("litellm_params") or {}).get("model", "")
                    if self._cooldown_tracker:
                        self._cooldown_tracker.on_success(model)
                except Exception as exc:
                    logger.warning("cooldown tracker on_success 异常: %s", exc)

            self.router.deployment_callback_on_failure = _on_failure
            self.router.deployment_callback_on_success = _on_success
```

- [ ] **Step 3: 在 `LiteLLMBridge` 加公开读方法**

在 `LiteLLMBridge` 类某处(建议 `initialize()` 之后、`completion()` 之前)加:

```python
    def get_cooldown_status(self) -> Dict[str, Any]:
        """返回所有 model 的 cooldown 状态(供 /admin/health 读)。"""
        if self._cooldown_tracker is None:
            return {}
        return self._cooldown_tracker.get_all_status()

    def get_cooldown_status_by_provider(self) -> Dict[str, int]:
        """按 provider 聚合状态(供 /metrics 上报 Prometheus)。

        Returns:
            {provider: 0|1} 字典,0=CLOSED, 1=OPEN。
        """
        if self._cooldown_tracker is None:
            return {}
        return self._cooldown_tracker.get_provider_states()
```

- [ ] **Step 4: import 冒烟测试(不真启 gateway,仅验证语法/import)**

命令:
```bash
python3 -c "
import sys
sys.path.insert(0,'aigateway-core/src')
sys.path.insert(0,'aigateway-api/src')
from aigateway_core.litellm_bridge import LiteLLMBridge, ProviderCooldownTracker
print('import OK')
print('ProviderCooldownTracker:', ProviderCooldownTracker)
print('get_cooldown_status:', hasattr(LiteLLMBridge, 'get_cooldown_status'))
print('get_cooldown_status_by_provider:', hasattr(LiteLLMBridge, 'get_cooldown_status_by_provider'))
"
```

预期输出:
```
import OK
ProviderCooldownTracker: <class 'aigateway_core.litellm_bridge.ProviderCooldownTracker'>
get_cooldown_status: True
get_cooldown_status_by_provider: True
```

- [ ] **Step 5: 提交**

```bash
git add aigateway-core/src/aigateway_core/litellm_bridge.py
git commit -m "feat(litellm-bridge): initialize() 传 allowed_fails/cooldown_time,注册 deployment callback 驱动 tracker

- 从 config.circuit_breaker 读 failure_threshold/recovery_timeout,映射到 litellm 参数
- 新增 get_cooldown_status() / get_cooldown_status_by_provider() 供 /metrics 与 admin 同步读"
```

---

### Task 4: routes.py 迁移 /metrics 和 /health 从 bridge 读

**Files:**
- Modify: `aigateway-api/src/aigateway_api/routes.py`

**Interfaces:**
- Consumes: `LiteLLMBridge.get_cooldown_status()` 和 `.get_cooldown_status_by_provider()`(Task 3)
- Produces: /metrics 上报的 circuit_breaker_state gauge 现在反映真实状态;/health 响应 `circuit_breakers` 字段来自 bridge

- [ ] **Step 1: 修改 /metrics 里的熔断状态上报块**

找到 routes.py:45-60 的这段:

```python
    try:
        from aigateway_api.main import app
        metrics_collector = getattr(app.state, "metrics_collector")
        circuit_breaker_factory = getattr(app.state, "circuit_breaker_factory")

        # 更新熔断器状态指标
        if circuit_breaker_factory:
            for provider, breaker in circuit_breaker_factory._breakers.items():
                if metrics_collector:
                    metrics_collector.set_circuit_breaker_state(
                        provider=provider,
                        state=breaker.get_state_value(),
                    )
```

改为:

```python
    try:
        from aigateway_api.main import app
        metrics_collector = getattr(app.state, "metrics_collector")
        litellm_bridge = getattr(app.state, "litellm_bridge", None)

        # 更新熔断器状态指标(从 litellm cooldown tracker 读,按 provider 聚合)
        if litellm_bridge and metrics_collector:
            provider_states = litellm_bridge.get_cooldown_status_by_provider()
            for provider, state in provider_states.items():
                metrics_collector.set_circuit_breaker_state(
                    provider=provider,
                    state=state,
                )
```

- [ ] **Step 2: 修改 /health 里的熔断状态构建块**

找到 routes.py:95-146 附近这段:

```python
    circuit_breaker_factory = getattr(s, "circuit_breaker_factory")
```
和
```python
    # 构建熔断器状态
    cb_status: Dict[str, Dict[str, Any]] = {}
    if circuit_breaker_factory:
        for provider, breaker in circuit_breaker_factory._breakers.items():
            cb_status[provider] = breaker.get_status()
```

第一处删除,第二处改为:

```python
    # 构建熔断器状态(从 litellm bridge tracker 读)
    cb_status: Dict[str, Dict[str, Any]] = {}
    litellm_bridge_for_cb = getattr(s, "litellm_bridge", None)
    if litellm_bridge_for_cb:
        cb_status = litellm_bridge_for_cb.get_cooldown_status()
```

- [ ] **Step 3: import 冒烟测试**

```bash
python3 -c "
import sys
sys.path.insert(0,'aigateway-core/src')
sys.path.insert(0,'aigateway-api/src')
from aigateway_api import routes
print('routes import OK')
"
```

预期:`routes import OK`,无 traceback。

- [ ] **Step 4: 提交**

```bash
git add aigateway-api/src/aigateway_api/routes.py
git commit -m "refactor(routes): /metrics 与 /health 从 litellm_bridge 读 cooldown 状态"
```

---

### Task 5: admin_routes.py 迁移 /admin/health

**Files:**
- Modify: `aigateway-api/src/aigateway_api/admin_routes.py`

**Interfaces:**
- Consumes: `LiteLLMBridge.get_cooldown_status()`
- Produces: `/admin/health` 响应 `data.circuit_breakers` 字段来自 bridge tracker

- [ ] **Step 1: 修改 admin_routes.py 中熔断状态展示**

找到 admin_routes.py:486 附近:

```python
    circuit_breaker_factory = getattr(s, "circuit_breaker_factory")
```

以及 line 533-536 附近:

```python
    # 熔断器状态
    cb_states: Dict[str, Any] = {}
    if circuit_breaker_factory:
        for provider, breaker in circuit_breaker_factory._breakers.items():
            cb_states[provider] = breaker.get_status()
```

第一处删除;第二处改为:

```python
    # 熔断器状态(从 litellm bridge tracker 读)
    cb_states: Dict[str, Any] = {}
    litellm_bridge_for_cb = getattr(s, "litellm_bridge", None)
    if litellm_bridge_for_cb:
        cb_states = litellm_bridge_for_cb.get_cooldown_status()
```

- [ ] **Step 2: 全仓 grep 确认 admin_routes.py 无残留 circuit_breaker_factory 引用**

```bash
grep -nE "circuit_breaker_factory|_breakers|CircuitBreakerFactory" aigateway-api/src/aigateway_api/admin_routes.py
```

预期:无输出。

- [ ] **Step 3: 提交**

```bash
git add aigateway-api/src/aigateway_api/admin_routes.py
git commit -m "refactor(admin): /admin/health 从 litellm_bridge 读 cooldown 状态"
```

---

### Task 6: main.py 删除 CircuitBreakerFactory 初始化 + app.state 注册

**Files:**
- Modify: `aigateway-api/src/aigateway_api/main.py`

**Interfaces:**
- Consumes: 无
- Produces: `app.state` 上不再有 `circuit_breaker_factory` 属性

- [ ] **Step 1: 删除 import**

找到 main.py:39:
```python
from aigateway_core.circuit_breaker import CircuitBreakerFactory
```
删除该行。

- [ ] **Step 2: 删除 CircuitBreakerFactory 初始化块(main.py:358-365)**

删除这一整块:
```python
    # 初始化 CircuitBreakerFactory
    cb_cfg = config_manager.get("circuit_breaker", {})
    cb_factory = CircuitBreakerFactory(
        failure_threshold=int(cb_cfg.get("failure_threshold", 5)) if cb_cfg else 5,
        recovery_timeout=int(cb_cfg.get("recovery_timeout", 60)) if cb_cfg else 60,
        long_open_alert_seconds=int(cb_cfg.get("long_open_alert_seconds", 300)) if cb_cfg else 300,
    )
    logger.info("CircuitBreakerFactory 初始化完成")
```

- [ ] **Step 3: 删除 app.state 注册(main.py:516)**

删除这行:
```python
    app.state.circuit_breaker_factory = cb_factory
```

- [ ] **Step 4: 找到 lifespan 中的 lifespan 文档注释里 "5. 初始化 CircuitBreakerFactory" 那行,一并清理**

搜索:
```bash
grep -n "CircuitBreakerFactory\|初始化 CircuitBreaker" aigateway-api/src/aigateway_api/main.py
```

如有 docstring 里的注释行(如 "5. 初始化 CircuitBreakerFactory"),删除该行。

- [ ] **Step 5: import 冒烟测试**

```bash
python3 -c "
import sys
sys.path.insert(0,'aigateway-core/src')
sys.path.insert(0,'aigateway-api/src')
from aigateway_api.main import create_app
print('create_app import OK')
"
```

预期:`create_app import OK`,无 `ImportError: cannot import name 'CircuitBreakerFactory'`。

- [ ] **Step 6: 提交**

```bash
git add aigateway-api/src/aigateway_api/main.py
git commit -m "refactor(main): 移除 CircuitBreakerFactory 初始化(交给 litellm 内置 cooldown)"
```

---

### Task 7: 删除 circuit_breaker.py + 清理 bridge 的 fallback_chain 死代码(可选)

**Files:**
- Delete: `aigateway-core/src/aigateway_core/circuit_breaker.py`
- Modify: `aigateway-core/src/aigateway_core/litellm_bridge.py`(可选,清理 fallback_chain 死循环)

**Interfaces:**
- Consumes: 无
- Produces: 无(纯删除)

- [ ] **Step 1: 全仓核查 circuit_breaker 无外部引用**

```bash
grep -rnE "from.*circuit_breaker|import.*CircuitBreaker" \
  aigateway-core/src/ aigateway-api/src/ tests/ 2>&1 | grep -v "^Binary" | head
```

预期:无匹配(应仅剩下这个即将被删的文件本身)。

- [ ] **Step 2: 删除 circuit_breaker.py**

```bash
git rm aigateway-core/src/aigateway_core/circuit_breaker.py
```

- [ ] **Step 3: 全仓核查 exceptions.py 里的 CircuitBreakerOpenError 是否还有引用**

```bash
grep -rn "CircuitBreakerOpenError" aigateway-core/src/ aigateway-api/src/ tests/ 2>&1 | head
```

如果只剩 exceptions.py 里的定义(无引用),把它从 exceptions.py 里一并删除;否则保留。

- [ ] **Step 4(可选,清理死代码)清理 completion() 里 fallback_chain 分支**

打开 litellm_bridge.py,搜索 `fallback_chain`:

```bash
grep -n "fallback_chain" aigateway-core/src/aigateway_core/litellm_bridge.py
```

`completion()` 函数签名里的 `fallback_chain: Optional[List[str]] = None` 参数**保留**(向后兼容);但函数体内 `if fallback_chain: for fb in fallback_chain: candidates.append(...)` 那段循环(约 line 538-543)可以简化为一行注释说明"litellm.Router 内部已处理 fallback,此参数已 deprecated,保留仅为兼容"。或直接保留,不动(此任务是可选清理,不是必需)。

**决定:此步骤 skip**,不动 completion() 循环,减小本次 PR 风险。fallback_chain 参数虽是死代码,但删了可能影响未来接入。留作后续 cleanup。

- [ ] **Step 5: 提交**

```bash
git add aigateway-core/src/aigateway_core/circuit_breaker.py aigateway-core/src/aigateway_core/exceptions.py
git commit -m "chore: 删除 circuit_breaker.py(439 行,从未接入 LLM 路径,由 litellm 内置 cooldown 替代)"
```

---

### Task 8: Docker 重建 + 集成验证

**Files:**
- 无代码改动,纯验证

**Interfaces:**
- 无

- [ ] **Step 1: Docker 重建 gateway**

```bash
sudo DOCKER_BUILDKIT=1 docker compose up -d --build gateway 2>&1 | tail -5
```

预期:`Container gateway2-gateway-1 Started`,无 error/traceback。

- [ ] **Step 2: 等 5 秒,验证 /health**

```bash
sleep 5
curl -s -w "\nHTTP: %{http_code}\n" http://localhost:8000/health | python3 -m json.tool 2>&1 | head -30
```

预期:HTTP 200,响应 JSON 含 `dependencies`、`plugins`,以及不再报错(cooldown 状态可能为空 `{}` 因为还没有失败请求)。

- [ ] **Step 3: 检查启动日志无 traceback**

```bash
sudo docker compose logs --tail=50 gateway 2>&1 | grep -iE "error|traceback|exception|circuit" | head
```

预期:无 ImportError,可能有 `ProviderCooldownTracker` 或"cooldown"相关的 info 日志。

- [ ] **Step 4: 触发失败请求,验证 cooldown 生效**

先看 config.yaml 里 test-broken-model 是否配置:

```bash
grep -nA5 "test-broken-model" config.yaml | head
```

如果存在(line 125 附近有),用它发请求:

```bash
for i in 1 2 3 4 5 6; do
  curl -s -X POST http://localhost:8000/v1/chat/completions \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer <admin-key>" \
    -d '{"model":"test-broken-model","messages":[{"role":"user","content":"hi"}]}' \
    -o /dev/null -w "req $i: %{http_code}\n"
  sleep 1
done
```

预期:前几次可能返回 5xx 或 fallback 到 agnes-2.0-flash(config.yaml:126 配了 fallback),累计失败达 5 次后 tracker 应记录该 model 为 OPEN。

**注意**:`<admin-key>` 用环境变量 `AI_GATEWAY_ADMIN_KEY` 值(前面已见 `gw-rRIop4dpcyJJNUTJbHmHpr9Bj3M11s5o`)。

- [ ] **Step 5: 验证 tracker 状态可见**

```bash
curl -s -H "Authorization: Bearer $AI_GATEWAY_ADMIN_KEY" \
  http://localhost:8000/admin/health | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(json.dumps(d.get('data',{}).get('circuit_breakers',{}), indent=2, ensure_ascii=False))
"
```

预期:如果失败请求触发了 tracker,应看到 model 名下的 state / failure_count / cooldown_until 等字段。可能因为 fallback 直接成功没触发失败回调,此时 `circuit_breakers` 为空——这是正常的(litellm 内部处理了 fallback 就不算 primary 失败)。

- [ ] **Step 6: /metrics 检查 circuit_breaker_state gauge 存在**

```bash
curl -s http://localhost:8000/metrics | grep -E "circuit_breaker_state" | head
```

预期:如果 tracker 有数据,能看到 `circuit_breaker_state{provider="..."} 0.0` 之类的行;若无数据(所有 provider 健康)则该指标可能不出现,这也 OK。

- [ ] **Step 7: 运行全部单元测试确认无回归**

```bash
export AI_GATEWAY_ADMIN_KEY=gw-rRIop4dpcyJJNUTJbHmHpr9Bj3M11s5o
python3 -m pytest tests/ --ignore=tests/test_template_routes.py --ignore=tests/e2e -q 2>&1 | tail -10
```

预期:全部通过。e2e 测试需要真实网络/服务,跳过。

- [ ] **Step 8: 提交(如果步骤 4-6 中发现任何微调需求)**

如上一步需要调整,再补 commit;否则本 Task 无需 commit,只是验证性。

---

### Task 9: 文档同步(CLAUDE.md + ARCHITECTURE_DIAGRAM.md)

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/ARCHITECTURE_DIAGRAM.md`
- (Optional) Delete: `docs/ARCHITECTURE_COMPARE.md`

**Interfaces:**
- 无(纯文档)

- [ ] **Step 1: CLAUDE.md 更新 Architecture Decisions**

找到 CLAUDE.md 里"CircuitBreaker 未接入 LLM 调用路径"那条(前面 grep 应可定位):

```bash
grep -n "CircuitBreaker" CLAUDE.md | head
```

把该条改为:

```markdown
- **CircuitBreaker 迁移至 litellm 内置 cooldown(2026-07-06)** — 自实现的 `circuit_breaker.py`(CircuitBreaker + CircuitBreakerFactory,439 行)从未接入 LLM 调用路径,已删除。改用 litellm 1.83.7 Router 内置 cooldown 机制(`allowed_fails` + `cooldown_time`,和 fallback 在同一调度链协同)。config.yaml `circuit_breaker:` 段字段向前兼容(`failure_threshold`→`allowed_fails`,`recovery_timeout`→`cooldown_time`)。`LiteLLMBridge` 内 `ProviderCooldownTracker` 通过 `deployment_callback_on_failure/success` 维护 per-model 状态,供 `/metrics` 与 `/admin/health` 同步读取(按 provider 聚合)。不实现 HALF-OPEN(litellm 无对应概念)。
```

- [ ] **Step 2: CLAUDE.md 更新 Architecture at a Glance 表**

找到表中 `circuit_breaker.py` 一行,删除该行(现文件已不存在)。

```bash
grep -n "circuit_breaker.py" CLAUDE.md
```

用 Edit 工具删除对应行。

- [ ] **Step 3: docs/ARCHITECTURE_DIAGRAM.md 更新 C.1 图注**

在 C.1 目标架构图里找到:

```
│    - CircuitBreaker 包裹                                               │
```

改为:

```
│    - litellm 内置 cooldown 熔断(allowed_fails/cooldown_time,          │
│      与 fallback 同一调度链协同)                                       │
```

- [ ] **Step 4: 删除临时对比文档(可选)**

```bash
git rm docs/ARCHITECTURE_COMPARE.md
```

理由:该文档是过程产物,C.1 已同步更新,不再需要。

- [ ] **Step 5: 提交文档同步**

```bash
git add CLAUDE.md docs/ARCHITECTURE_DIAGRAM.md
# 若上一步删了 COMPARE:
# git add docs/ARCHITECTURE_COMPARE.md  (git rm 已入 stage)
git commit -m "docs: 同步 CircuitBreaker → litellm cooldown 迁移

- CLAUDE.md: 更新 Architecture Decisions 条目;Key Files 表移除 circuit_breaker.py
- ARCHITECTURE_DIAGRAM.md: C.1 图注 'CircuitBreaker 包裹' → 'litellm 内置 cooldown 熔断'
- 删除临时 ARCHITECTURE_COMPARE.md(过程产物,已闭环)"
```

- [ ] **Step 6: push 到 origin/main(workflow rule 4)**

```bash
git push origin main 2>&1 | tail -3
```

预期:推送成功,`... -> main`。

---

## Self-Review 记录

- **Placeholder 扫描**:无 TBD/TODO/XXX/vague 步骤,所有 code block 完整。
- **Spec 覆盖**:
  - 目标 1(启用 litellm cooldown + config 覆盖)→ Task 3
  - 目标 2(删 circuit_breaker.py)→ Task 6/7
  - 目标 3(/metrics 与 admin 反映真实状态)→ Task 4/5
  - 目标 4(config.yaml 向前兼容)→ Task 3 中已复用 `circuit_breaker:` 段
  - 目标 5(不引入手动拉黑)→ ProviderCooldownTracker 无 `force_open`/`reset` API
  - 决策 1(状态暴露选项 A)→ Task 3 callback + Task 4/5 同步读
  - 决策 2(复用 config 段)→ Task 3
  - 决策 3(纯删除不留 shim)→ Task 7
  - `long_open_alert_seconds` 保留告警日志 → Task 2 tracker 内 `logger.error`
- **Type 一致性**:
  - `ProviderCooldownTracker(allowed_fails, cooldown_time, long_open_alert_seconds=300)`:Task 1 契约 + Task 2 实现 + Task 3 调用参数完全对齐。
  - `on_failure(model: str) / on_success(model: str)`:Task 1 契约 + Task 2 实现 + Task 3 callback 都用同名同签名。
  - `get_all_status() / get_provider_states()`:Task 1 契约 + Task 2 实现 + Task 3 bridge 包装 + Task 4/5 消费点全部一致。
- **验收标准覆盖**:Task 8 步骤 1-7 覆盖 spec 中 7 条验收标准。
