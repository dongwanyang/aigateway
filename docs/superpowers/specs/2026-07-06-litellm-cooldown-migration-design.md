# 迁移 CircuitBreaker 到 litellm 内置 cooldown

> 日期:2026-07-06
> 作者:与主会话协作
> 状态:待用户 review

## 背景

当前项目有一套自实现的 `CircuitBreaker` + `CircuitBreakerFactory`(`aigateway-core/src/aigateway_core/circuit_breaker.py`,439 行),设计为 per-provider 熔断器。但通过代码核查(2026-07-06)发现:

1. **该实现从未接入 LLM 调用路径**:`litellm_bridge.py` 零 `circuit_breaker` 引用,`.allow_request()` / `.record_success()` / `.record_failure()` / `.protect()` 全仓无外部调用。熔断器状态永远停在 CLOSED,`/metrics` 与 admin 界面展示的熔断状态是"死的",不反映真实健康度。
2. **litellm 1.83.7 原生自带 cooldown 熔断机制**,且与 fallback 在**同一条调度链**里协同:一个 deployment 连续失败达 `allowed_fails` 阈值 → 加入 cooldown 列表 → `_filter_cooldown_deployments` 自动从可用 deployment 中过滤 → 冷却 `cooldown_time` 秒后恢复。这正是熔断的语义,只是名字叫 cooldown。
3. **当前 bridge 初始化 Router 时未显式启用**:`Router(model_list=..., routing_strategy=..., num_retries=3)` 只传了 3 个参数,`allowed_fails` / `cooldown_time` 均走 litellm 默认值(`cooldown_time` 默认仅 1 秒,基本等于没熔断)。

因此当前架构中"熔断"与"fallback"是两条互不相干的机制:
- 我们的 `CircuitBreaker`:死代码,不参与调度
- litellm 内置 fallback:在 `Router.acompletion` 里生效,是真正跑着的
- litellm 内置 cooldown:开着但参数是默认值,失效等同于关

用户诉求是"把熔断和 fallback 合并"—— litellm 本就把这两件事做在同一条调度链里,最优解是**启用 litellm 内置 cooldown,删掉我们那套从未生效的 CircuitBreaker**,让熔断和 fallback 由 litellm 统一管理。

## 目标

1. 启用 litellm Router 内置 cooldown,`allowed_fails` / `cooldown_time` 有合理默认值且可由 `config.yaml` 覆盖。
2. 删除 `aigateway-core/src/aigateway_core/circuit_breaker.py` 及所有相关引用。
3. `/metrics` 与 admin 的"熔断器状态"改为反映 litellm 真实的 cooldown 状态。
4. `config.yaml` 的 `circuit_breaker:` 段保留、语义映射到 litellm 参数(向前兼容,不破坏运维现有配置)。
5. 不引入手动"运维一键拉黑 provider"能力(用户明确说不需要,litellm 纯自动即可)。

## 非目标

- 不修其他"缺陷"(缺陷 A: RAG 在 cache 之前 / 缺陷 C: cache 命中不扣配额)。这些在其他 spec 处理。
- 不改 fallback 机制本身。litellm 内置 fallback 已够用,继续用。
- 不新增手动熔断/reset 的 admin 接口。
- 不接入 litellm 的其他 pre_call_checks(如 context window filtering)。

## 架构决策

### 决策 1:cooldown 状态怎么暴露给 `/metrics`(选项 A)

litellm Router 没有暴露"当前哪些 deployment 在 cooldown"的同步公开接口。备选:

- **选项 A(采纳)**:在 bridge 内注册 litellm 的两个 callback:`async_deployment_callback_on_failure` 和 `async_deployment_callback_on_success`,自己维护一份 `{provider: cooldown_state}` 映射(dict + 简单结构)。`/metrics` 同步读这份映射,O(1)。
- 选项 B(未采纳):`/metrics` 每次 async 调 `async_get_healthy_deployments`,和 `get_model_list` 做差集算出 cooldown 列表。Prometheus scrape 高频,每次算成本高;且 `/metrics` handler 要改成 async。

**选择 A 的理由**:`/metrics` 高频访问,自己维护小映射性能可控;与 litellm 的 async 世界解耦,`/metrics` handler 保持同步简单。

### 决策 2:配置层复用 `circuit_breaker:` 段(向前兼容)

当前 `config.yaml` 有:
```yaml
circuit_breaker:
  failure_threshold: 5
  recovery_timeout: 60
  long_open_alert_seconds: 300
```

- **复用该段(采纳)**:保留 key `circuit_breaker`,把内部字段映射到 litellm 参数:
  - `failure_threshold` → `allowed_fails`
  - `recovery_timeout` → `cooldown_time`(litellm 命名不同但语义相同)
  - `long_open_alert_seconds` → 保留但**不再用**(litellm 无对应参数;或者用于我们的告警日志,见"其他"节)
- 未采纳:改名 `litellm_cooldown:` — 会破坏运维现有配置。

**代码内置默认值**(config 未提供时用):
- `allowed_fails`:5(与原 `failure_threshold` 默认一致)
- `cooldown_time`:60 秒(与原 `recovery_timeout` 默认一致,而非 litellm 默认的 1 秒)

**允许用户覆盖**:用户改 `config.yaml` 里 `circuit_breaker.failure_threshold` 或 `circuit_breaker.recovery_timeout` 即生效(下次启动/热重载后)。

### 决策 3:纯删除,不保留 shim

`circuit_breaker.py` 整个文件删除,不留 deprecation shim。理由:

- 该类**从未被 LLM 调用路径引用**(核实过),没有下游依赖需要平滑迁移。
- 唯一的消费方是 `routes.py` / `admin_routes.py` / `main.py` 里的观测/展示代码,这些点在本次改动内一起迁移。
- 保留 shim 只会让代码更混乱。

## 详细设计

### 组件 1:`ProviderCooldownTracker`(新增)

位置:`aigateway-core/src/aigateway_core/litellm_bridge.py` 内(不单独开文件,与 bridge 紧耦合)。

职责:

- 维护 `{model_name: CooldownEntry}` 映射,`CooldownEntry` 含:`state`(CLOSED/OPEN)、`failure_count`、`last_failure_time`、`cooldown_until`(epoch)、`last_success_time`。
- 提供 `on_failure(model_name, exception)` 和 `on_success(model_name)` 方法,由 litellm callback 触发。
- 提供 `get_all_status() -> Dict[str, Dict]` 供 `/metrics` 和 admin 同步读。
- 提供 `get_provider_states() -> Dict[str, int]` 供 Prometheus 上报(聚合 model 到 provider,状态取最严重值:任何一个 model OPEN → provider OPEN)。

**为什么按 model 而非 provider 记录**:litellm callback 传的是 `deployment` / `model` 粒度,不是 provider 粒度。要拿到 provider 得从 model 名反查(bridge 已有 `_extract_provider(model)` 辅助函数,复用)。

**状态转换**(简化,与原 CircuitBreaker 三态对齐):

```
CLOSED --(连续失败达 allowed_fails)--> OPEN
OPEN   --(等待 cooldown_time 秒)-----> CLOSED(下次成功触发)
```

不实现 HALF-OPEN。litellm 内部有"探测请求"机制(cooldown_time 后 deployment 自动重新可用,失败再入 cooldown),Tracker 只反映"当前是否在 cooldown 期",不追加逻辑。

### 组件 2:`LiteLLMBridge` 改动

**改动 1:初始化 Router 时传 cooldown 参数**(litellm_bridge.py:176)

```python
# 读取 config 里的 circuit_breaker 段(向前兼容),映射到 litellm 参数
cb_cfg = self.config.get("circuit_breaker", {}) if isinstance(self.config, dict) else {}
allowed_fails = cb_cfg.get("failure_threshold", 5)      # 默认 5
cooldown_time = cb_cfg.get("recovery_timeout", 60)      # 默认 60 秒

self.router = Router(
    model_list=model_list,
    routing_strategy=routing_strategy_config,
    num_retries=...,
    allowed_fails=allowed_fails,           # 新增
    cooldown_time=cooldown_time,           # 新增
)
```

**改动 2:注册 callback,维护 tracker**(litellm_bridge.py 初始化尾部)

litellm callback 有两种注册方式:一是 `Router` 实例的 `deployment_callback_on_failure` 属性,二是全局 `litellm.callbacks = [...]`。用前者(实例作用域,不污染全局)。

```python
self._cooldown_tracker = ProviderCooldownTracker(
    allowed_fails=allowed_fails,
    cooldown_time=cooldown_time,
)
self.router.deployment_callback_on_failure = self._on_deployment_failure
self.router.deployment_callback_on_success = self._on_deployment_success

async def _on_deployment_failure(self, kwargs, completion_response, start_time, end_time):
    model = kwargs.get("model", "") or (kwargs.get("litellm_params", {}) or {}).get("model", "")
    self._cooldown_tracker.on_failure(model)

async def _on_deployment_success(self, kwargs, completion_response, start_time, end_time):
    model = kwargs.get("model", "") or (kwargs.get("litellm_params", {}) or {}).get("model", "")
    self._cooldown_tracker.on_success(model)
```

**注意**:litellm callback 签名以库文档为准,实施阶段以实际签名对齐。

**改动 3:暴露 tracker 供外部读**

`LiteLLMBridge.get_cooldown_status() -> Dict[str, Dict]` — 返回 tracker 的当前快照。routes.py / admin_routes.py 读它替代原 `cb_factory._breakers` 遍历。

**改动 4:移除 fallback_chain 死代码**

litellm_bridge.py:538 的 `if fallback_chain:` 分支是死代码(dispatcher 从不传)。本次一并清理,`completion()` 签名可以移除 `fallback_chain` 参数(或保留参数为 None-op 做向后兼容)。倾向直接移除。

### 组件 3:`main.py` 改动

- 删除 `from aigateway_core.circuit_breaker import CircuitBreakerFactory`(line 39)。
- 删除 `CircuitBreakerFactory` 初始化块(line 358-365)。
- 删除 `app.state.circuit_breaker_factory = cb_factory`(line 516)。
- **不新增** app.state 引用 —— 各消费方通过 `app.state.litellm_bridge.get_cooldown_status()` 拿状态即可。

### 组件 4:`routes.py` 改动(/metrics 和 /health)

当前(line 49-55, 143-144):
```python
circuit_breaker_factory = getattr(app.state, "circuit_breaker_factory")
if circuit_breaker_factory:
    for provider, breaker in circuit_breaker_factory._breakers.items():
        metrics_collector.set_circuit_breaker_state(provider, breaker.get_state_value())
```

改为:
```python
bridge = getattr(app.state, "litellm_bridge", None)
if bridge is not None:
    for provider, state in bridge.get_cooldown_status_by_provider().items():
        metrics_collector.set_circuit_breaker_state(provider, state)
```

Prometheus 指标名 `set_circuit_breaker_state` 保留(向前兼容,面板不用改);语义不变(0=CLOSED, 1=OPEN, 2=HALF_OPEN—— 只用 0/1,litellm 无 HALF_OPEN 概念)。

### 组件 5:`admin_routes.py` 改动

当前(line 486, 533-541)读 `circuit_breaker_factory._breakers`,展示到 admin 界面 `/admin/health` 里的 `circuit_breakers` 字段。

改为读 `bridge.get_cooldown_status()`,响应字段 key 名 `circuit_breakers` **保留**(前端不改)。返回结构对齐原格式:

```json
{
  "circuit_breakers": {
    "openai": {"state": "CLOSED", "state_value": 0, "failure_count": 0, ...},
    "anthropic": {"state": "OPEN", "state_value": 1, "cooldown_until": 1234567890, ...}
  }
}
```

### 组件 6:`metrics.py` — `set_circuit_breaker_state`

**不改**。方法签名/Prometheus 指标名不变,仍用 provider 级别的 0/1/2 状态值。tracker 内部把 model 级状态聚合成 provider 级再上报。

### 组件 7:测试

- **删除** `tests/test_circuit_breaker.py`(如存在)—— 测的是被删的类。
- **新增** `tests/test_provider_cooldown_tracker.py`:
  - CLOSED → 累计失败 → 达 allowed_fails → OPEN
  - OPEN → 等 cooldown_time 秒 → 自动 CLOSED(下次 success 触发)
  - success 重置 failure_count
  - 多 model 独立跟踪
  - `get_all_status()` 返回结构正确
  - `get_provider_states()` 正确聚合(任一 model OPEN → provider OPEN)
- **集成测试**:mock litellm callback,验证 bridge 收到失败/成功事件时 tracker 状态正确更新。
- **手动验证**:启动 gateway,配一个必挂的 provider(如 `providers.deepseek.base_url = http://nonexistent`),连打 5 次请求,`curl /admin/health` 应看到该 provider `state: OPEN`。

## 其他

### `long_open_alert_seconds` 字段处理

原 `CircuitBreaker.check_long_open()` 用它做"OPEN 超过 5 分钟发告警"。litellm 无对应能力。

**决定:保留字段,tracker 内部检查 OPEN 时长超过阈值时 `logger.error` 一条**(不阻断请求)。理由:运维可能已在监控这条日志,行为保持一致。实施方式:tracker 在 `get_all_status()` 或 `on_success/on_failure` 触发时顺便检查(不额外起后台任务),简单可靠。默认值:300 秒。

### 迁移风险

1. **litellm callback 签名可能变**:1.83.7 的签名以运行时实测为准,实施时用最小 mock 先跑通 callback 触发。
2. **model → provider 映射**:bridge `_extract_provider(model)` 需要覆盖所有配置的 provider(agnes / deepseek / openai / anthropic / …),这个映射本就存在,复用即可。
3. **测试环境依赖**:测试熔断需要真实触发失败,不能只靠 mock。建议用 `tests/test_provider_cooldown_tracker.py` 单元测 tracker 本身,集成测用 `providers.test-broken-model` 那类必挂配置(config.yaml:125 已有 `test-broken-model` 例子)。

### 向前兼容

- `config.yaml` 用户无需改任何字段。
- Prometheus 指标名 / admin API 响应结构完全一致。
- 唯一 break 是内部导入:`from aigateway_core.circuit_breaker import ...` 会失败。全仓核查后此 import 只在 main.py 一处,已在改动内。

## 代码量估算

- 删除:`circuit_breaker.py`(439 行)、`main.py` 相关初始化(约 15 行)、`test_circuit_breaker.py`(如存在)
- 新增:`ProviderCooldownTracker` 类(约 100 行,含状态转换与聚合)、`test_provider_cooldown_tracker.py`(约 120 行)
- 修改:`litellm_bridge.py`(初始化 + callback 注册,约 30 行)、`routes.py`(2 处 5 行)、`admin_routes.py`(1 处 8 行)
- 净删除约 300 行代码。

## 验收标准

1. ✅ 启动 gateway,`/health` 返回 `circuit_breakers` 字段(空 dict 或 CLOSED 状态)。
2. ✅ 配置一个必挂 provider,连打 5 次请求,`/admin/health` 中该 provider 变 OPEN。
3. ✅ 等 60 秒后,下次成功请求触发 provider 回 CLOSED。
4. ✅ `curl /metrics | grep circuit_breaker_state` 反映真实 provider 状态(不再永远 0)。
5. ✅ Docker 重建 gateway 无 import 错误、启动日志无 traceback。
6. ✅ `python3 -m pytest tests/test_provider_cooldown_tracker.py -v` 全绿。
7. ✅ 现有测试全部通过(不引入回归)。

## 文档同步

改动落地后,同步修改:

- **CLAUDE.md**:
  - Architecture Decisions 中"CircuitBreaker 未接入 LLM 调用路径" 条目 → 改为"CircuitBreaker 已迁移至 litellm 内置 cooldown(YYYY-MM-DD)"。
  - Architecture at a Glance 中 `circuit_breaker.py` 一行移除。
  - Path 3 或 Bridge 描述加一句"litellm cooldown 熔断,`allowed_fails`+`cooldown_time` 通过 `config.yaml` 的 `circuit_breaker:` 段配置"。
- **docs/ARCHITECTURE_DIAGRAM.md**:
  - C.1 图注 `- CircuitBreaker 包裹` → 改为 `- litellm 内置 cooldown 熔断(allowed_fails/cooldown_time)`。
  - C.2 表移除 `CircuitBreakerFactory` 相关行(如有)。
- **docs/ARCHITECTURE_COMPARE.md**(临时对比文档):完成后可删,或标注"已随本次改动闭环"。
