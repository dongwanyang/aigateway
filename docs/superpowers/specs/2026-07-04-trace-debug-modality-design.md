# 全链路 trace_id + 分维度 Debug 开关 + 控制台插件分栏 设计

- 日期:2026-07-04
- 状态:Draft(待用户 review)
- 作者:CodeBuddy brainstorming
- 涉及三件事(用户明确为"三件事",但共享一套数据通道):
  1. 全链路 trace_id —— 修复当前一次请求生成 3+ 个 trace_id 的 bug,让 `/admin/trace/{id}` 能看到完整流程
  2. 分维度 Debug 开关 —— 替换粗暴的 `debug_mode` 总开关,改为 5 维度 + 14 插件开关
  3. 控制台 `/plugins` 页上下分栏 —— 按 `pipeline_kind` 分"理解/生成"两区,二级保留功能类别

## 背景与现状(为什么"太乱")

经代码勘查确认的问题:

### trace_id 断链
- `PipelineContext.trace_id`(`context.py:61`)默认 factory `uuid.uuid4().hex`,**每次构造 ctx 都生成新 id**
- dispatcher 在 `dispatcher.py:293`(understanding)和 `447`(generation)构造 ctx 时**不传 trace_id**
- 共用前置 `_apply_pii_detection` / `_apply_media_optimization`(`openai_compat.py:345/396/531`)各构造自己的 ctx,**又是新 id**
- `_record_request_log`(`openai_compat.py:275`)用 `getattr(request.state, "trace_id", "") or uuid.uuid4().hex[:12]`,但**没有任何地方写 `request.state.trace_id`**,所以永远 mint 新的 12 位 id
- 结果:一次 HTTP 请求产生 3+ 个不同 trace_id,`/admin/logs` 显示的和插件日志里的对不上
- `TracingManager.create_request_span`(`tracing.py:176`)**从未被调用**;`create_plugin_span`(`tracing.py:197`)返回的是 dict,不是真 OTel span,6 个 gen-opt 插件调它是空操作

### 插件注册 ≠ 实际执行
- `pii_detector`/`prompt_cache`/`semantic_cache`/`model_router`/`prompt_compress`/`media_optimizer` 注册为 understanding 插件,但 dispatcher 用 `_skip_names`(`dispatcher.py:300`)在 engine 循环里跳过它们,改成内联执行
- engine 循环实际只跑 `rag_retriever` + `conv_compressor`
- `ModelRouterPlugin`(`pipeline.py:611`)是文档标注的 `[DEPRECATED 空壳]`,只为 `prompt_compress.depends_on` 保留
- `prompt_compress` 现在是 dispatcher 内联执行,不在 engine 循环

### Debug 开关粒度粗
- 全局 `debug_mode`(`config.yaml:190`)只控制:强制 DEBUG 日志级别(`main.py:227`)、5xx 响应回显 detail(`main.py:79`)、admin 热重载切换(`admin_routes.py:865`)
- **没有任何插件级 debug 开关**;`PluginRegistration`(`plugin_registry.py:38`)无 debug 字段
- 插件日志走 stdlib `logger`,structlog 的 `ContextInjectProcessor`(`logger.py:58`)因 `log_with_context()` 在请求流里不被调用,trace_id 不会自动注入插件日志

## 总体思路:方案 A —— 统一 TraceEvent 通道

三件事共享一个核心数据结构 `TraceEvent` 和一个收集器 `TraceCollector`:

- trace_id 那件事 = 修 mint 点 + 所有埋点统一进 collector
- debug 那件事 = collector 决定要不要收 `kind=debug` 事件 + 要不要填 payload
- 控制台分栏那件事 = 纯前端,和通道无关,但 trace 详情弹窗复用同一份数据

不引入 OpenTelemetry collector / Jaeger / Tempo(用户明确"和现在的差不多")。

---

## 第 1 部分:TraceEvent 通道 + trace_id 全链路

### 1.1 数据结构

新文件 `aigateway-core/src/aigateway_core/trace_event.py`:

```python
from dataclasses import dataclass, field
from typing import Literal
import time, uuid
from contextvars import ContextVar

@dataclass
class TraceEvent:
    trace_id: str
    ts: float                 # time.monotonic() 用于排序;wall clock 由 collector 统一打
    stage: str                # "auth"|"dispatch"|"pii"|"media"|"cache"|"bridge"|"quota"|插件名
    kind: Literal["stage", "plugin", "debug"]
    name: str                 # 如 "prompt_cache.lookup" / "pii_detector.sanitize" / "bridge.completion"
    duration_ms: float | None
    status: Literal["ok", "skip", "error"]
    payload: dict | None = None   # 仅 debug 事件或对应开关开时填

class TraceCollector:
    """进程内按 trace_id 累积事件,请求结束落 Redis。"""
    _current: ContextVar["TraceCollector | None"] = ContextVar("trace_collector", default=None)

    def __init__(self, trace_id: str):
        self.trace_id = trace_id
        self.events: list[TraceEvent] = []
        self._wall_start = time.time()

    @classmethod
    def current(cls) -> "TraceCollector | None":
        return cls._current.get()

    @classmethod
    def start(cls, trace_id: str) -> "TraceCollector":
        c = cls(trace_id)
        cls._current.set(c)
        return c

    def emit(self, ev: TraceEvent) -> None:
        self.events.append(ev)

    def flush(self, redis) -> None:
        """请求结束时调用,写 Redis hash。"""
        # 写 aigateway:trace:{trace_id},字段 events=JSON 数组,meta=摘要
        # TTL 7 天
        ...
```

### 1.2 trace_id 全链路绑定(修"3 个 mint"bug)

新增 ASGI 中间件 `aigateway-api/src/aigateway_api/trace_middleware.py`:

```python
class TraceMiddleware:
    async def __call__(self, request, call_next):
        trace_id = request.headers.get("x-trace-id") or uuid.uuid4().hex
        request.state.trace_id = trace_id
        collector = TraceCollector.start(trace_id)
        try:
            resp = await call_next(request)
        finally:
            await collector.flush(request.app.state.redis)  # 异步 flush
        resp.headers["x-trace-id"] = trace_id
        return resp
```

挂载位置:`main.py` 的 `create_app` 里,在 auth_middleware 之前(保证 auth 也能记 trace 事件)。

### 1.3 删 `PipelineContext.trace_id` 默认 factory

`context.py:61`:

```python
# 之前
trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
# 之后
trace_id: str  # 必传,无默认值
```

构造点改为从 `request.state.trace_id` 或 `TraceCollector.current().trace_id` 取:

- `dispatcher.py:293`(understanding ctx)
- `dispatcher.py:447`(generation ctx)
- `openai_compat.py:345`(`_apply_pii_detection` 的 ctx)
- `openai_compat.py:396` / `531`(`_apply_media_optimization` 的 ctx)

这样 3+ 个 ctx 必然共享同一个 trace_id。

### 1.4 logger 自动注入 trace_id

`logger.py:58` 的 `ContextInjectProcessor` 改为优先读 `TraceCollector.current().trace_id`,fallback 到现有 ContextVar。这样**所有 stdlib `logger.debug/info` 调用自动带 trace_id**,插件无需显式写。解决"插件日志无法和 admin log 交叉引用"痛点。

### 1.5 埋点统一迁移

把现有 11+ 处手工 `ctx.add_plugin_trace(...)` 和 6 个假 `create_plugin_span` 全部改为 `collector.emit(TraceEvent(...))`:

| 现有埋点 | 位置 | 迁移后 |
|---|---|---|
| engine 循环每插件 | `pipeline.py:161,177,189` | `emit(kind="plugin", stage=plugin.name)` |
| dispatcher 内联 cache/quota/compress/auth/media | `dispatcher.py:196,204,215,339,361,382,392,396,567,579,715` | `emit(kind="stage", stage="cache"/"quota"/...)` |
| 6 个 gen-opt 插件 `create_plugin_span` | `ai_director_plugin.py:104` 等 | 删除假 span,改 `emit(kind="plugin")` |
| `_run_engine_filtered` 的 add_plugin_trace | `dispatcher.py:929,933` | 走 collector |

`PipelineContext.add_plugin_trace`(`context.py:345`)保留为兼容包装(内部转 emit),或直接删除所有调用点后移除。**采用直接删除** —— 一次性清理干净。

### 1.6 `/admin/trace/{trace_id}` 接口升级

`admin_routes.py:1044` 现有接口改为返回:

```json
{
  "trace_id": "...",
  "events": [
    {"ts": 0.0, "stage": "auth", "kind": "stage", "name": "auth.verify", "duration_ms": 1.2, "status": "ok"},
    {"ts": 1.5, "stage": "pii_detector", "kind": "plugin", "name": "pii_detector.sanitize", "duration_ms": 3.1, "status": "ok", "payload": null},
    ...
  ],
  "meta": {"total_ms": 450, "model": "...", "cache_hit": false, "status": 200}
}
```

`plugin_trace` 字段名保留为别名(向后兼容旧前端),值指向 `events` 中 `kind=plugin` 的子集。

### 1.7 Redis key 设计

- 新 key:`aigateway:trace:{trace_id}` —— hash,字段 `events`(JSON 数组)+ `meta`(JSON),TTL 7 天
- 旧 key `aigateway:logs:requests`(ZSET)保留(列表页用),其 entry 的 `plugin_trace` 字段在 PR1/PR2 期保持双写(同时写旧字段和新 events);**PR3 前端切到新接口后,删除 entry 中的 `plugin_trace` 字段及双写逻辑**。

### 1.8 5xx 错误 detail

`main.py:79` 的 `_is_debug_mode()` 逻辑改为**固定回显 redacted detail**(脱敏后始终返回),不再受任何 debug 开关控制。删除 `_is_debug_mode` 函数。

---

## 第 2 部分:5 维度 Debug 开关

### 2.1 config.yaml 新结构

替换现有 `debug_mode: false`(单行,`config.yaml:190`):

```yaml
debug:
  frontend: false          # 前端 control-panel 浏览器日志
  entry: false             # auth + dispatcher + 共用前置(PII/media) + quota + prompt_compress(内联)
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

`model_router` 不列(已退役,见 2.5)。`prompt_compress` 不在 `per_plugin`(保留 dispatcher 内联,归 `entry` 档,见 2.4)。开关总数:4 大区(frontend/entry/cache/bridge)+ 1 插件总开关 + 11 插件 = **16 个**。

### 2.2 getCategory 映射补全(前端依赖)

`Plugins.tsx:92` 的 `getCategory(name)` 现有硬编码映射需确认覆盖所有 12 个保留插件名(11 个 engine 插件 + prompt_compress 注册仍在)。本次实现时检查并补全:

- 理解侧:`prompt_cache`/`semantic_cache`→缓存,`pii_detector`→安全,`conv_compressor`→性能,`rag_retriever`/`prompt_compress`→其他
- 生成侧:`gen_model_router`→路由,`token_compressor`/`cost_tracker`→性能,`ai_director`/`intent_evaluator`/`draft_generator`→其他

未命中的插件名一律落"其他"。`getCategory` 函数本身不改逻辑,只补映射表。

### 2.3 DebugConfig dataclass + 热重载

新文件 `aigateway-core/src/aigateway_core/debug_config.py`:

```python
@dataclass
class DebugConfig:
    frontend: bool = False
    entry: bool = False
    cache: bool = False
    bridge: bool = False
    plugins_enabled: bool = False
    per_plugin: dict[str, bool] = field(default_factory=dict)

    @classmethod
    def from_yaml(cls, d: dict) -> "DebugConfig": ...

    def is_plugin_debug(self, name: str) -> bool:
        return self.plugins_enabled and self.per_plugin.get(name, False)
```

走 `ConfigManager.on_reload()` 回调(复用 `GenerationOptimizationConfigWatcher` 模式,`generation_optimization/config.py`)。atomic swap,无锁读。

### 2.3 Debug 日志产出(挂第 1 部分通道)

- `kind=debug` 事件只在对应开关开启时 emit
- `payload` 字段:开关关时为 `None`(只留耗时);开关开时填 `{input_summary, output_summary, key_vars}`(各插件自定,长度截断,敏感字段脱敏 —— 复用 `PIIDetector` 脱敏)
- 各维度判断点:
  - `entry`:auth/dispatcher/共用前置/quota/**prompt_compress 内联** 代码读 `debug_config.entry`
  - `cache`:`CacheManager` 所有 get/set 读 `debug_config.cache`
  - `bridge`:`LiteLLMBridge.completion` / `completion_stream` + CircuitBreaker + `ModelRouterStrategy` 读 `debug_config.bridge`
  - 插件:engine 循环或插件 `execute` 内读 `debug_config.is_plugin_debug(self.name)`(prompt_compress 不在此列,见 2.4)
- 前端 `frontend` 维度特殊:control-panel 启动时 fetch `/admin/config/debug` 拿 `frontend` 值,本地控制 `console.debug` + 是否打印 fetch 请求/响应;不经过后端 Redis

### 2.4 prompt_compress 保留在 dispatcher 内联(用户决定)

用户明确"提示词压缩也还放在 dispatcher",不挪回 engine:

- `dispatcher.py` 的 prompt_compress 内联调用 + 手工埋点**保留**,埋点迁移时改成 `emit(kind="stage", stage="compress")`
- `pipeline.py` 的 `_skip_names`(`dispatcher.py:300`)**继续包含 `prompt_compress`**(engine 不跑它)
- `PromptCompressPlugin.execute()` 仍是被 skip 的死代码(保留注册仅为兼容,与 `model_router` 不同的是它有真实内联实现,不退役)
- 归 `entry` debug 档(无独立 per_plugin 开关)
- **/plugins 页**:保留卡片(注册仍在,有启用/禁用 toggle),但**不显示 Debug 按钮**(因为 debug 走 entry 大区开关,不是 per_plugin)
- `prompt_compress.depends_on` 确认摘掉 `model_router`(见 2.5)

### 2.5 model_router 彻底退役(额外代码改动)

- 删除 `ModelRouterPlugin` 类(`pipeline.py:611-652`)
- 删除 `_register_builtin_plugins` 中的注册(`pipeline.py:904`)
- `prompt_compress.depends_on` 去掉 `model_router`(原来是 `["model_router", ...]`,实际已是 `["semantic_cache"]`,确认即可)
- dispatcher `_skip_names`(`dispatcher.py:300`)去掉 `model_router`
- 前端 `/plugins` 不显示

### 2.6 删除旧 debug_mode

- `config.yaml:190` 删 `debug_mode: false`
- `config.py:181-208` 删 debug_mode 相关归一化(production 强制 False 那段)
- `main.py:227-238` 删"debug_mode 强制 DEBUG 级别"逻辑(log_level 改为只听 config.yaml + env)
- `admin_routes.py:865-943` 的 `/admin/config/hot_reload` 改为读写 `debug:` 段(替换 debug_mode 读写)
- `AI_GATEWAY_ENV=production` 的安全网保留(强制 log_level≥INFO),但不再关联 debug_mode

---

## 第 3 部分:控制台 UI 改造

### 3.1 /plugins 页上下分栏(`Plugins.tsx`)

现 `Plugins.tsx:306` 按 `['缓存','安全','性能','路由','其他']` 硬编码分组。改为两级:

```
一级: pipeline_kind ∈ {understanding, generation}
二级: getCategory(name) ∈ {缓存, 安全, 性能, 路由, 其他}(现有函数不动)
```

布局:

```
┌─ 理解管道(8 插件,删 model_router 后 7)──┐
│  [缓存] prompt_cache, semantic_cache      │
│  [安全] pii_detector                       │
│  [性能] conv_compressor                    │
│  [其他] rag_retriever, prompt_compress     │
└────────────────────────────────────────────┘
┌─ 生成管道(6 插件)─────────────────────┐
│  [路由] gen_model_router                   │
│  [性能] token_compressor, cost_tracker     │
│  [其他] ai_director, intent_evaluator,     │
│         draft_generator                    │
└────────────────────────────────────────────┘
```

- 外层循环 `['understanding','generation']`,内层循环 5 类
- 每张插件卡保留:图标 + 名称 + 「理解/生成」badge + 描述 + 启用 toggle
- **新增**:启用 toggle 旁加 Debug toggle(虫子图标),绑定 `plugin.debug` 字段。**例外**:`prompt_compress` 卡片不显示 Debug 按钮(它保留 dispatcher 内联,debug 走 entry 大区开关,见 2.4);其余 10 个 engine 插件 + `media_optimizer`(若启用)显示 Debug 按钮
- `model_router` 不显示(后端已不返回,见 2.5)
- `/admin/plugins` 接口返回的每个 plugin 增加 `debug` 字段;`prompt_compress` 的 `debug` 字段固定返回 `null`(前端据此隐藏按钮)

### 3.2 /config 页调试卡片(`Config.tsx` 或对应页)

- 删除现有"调试模式"开关栏
- 新增"调试日志"卡片,5 个开关垂直排列:

| 开关 | 说明 |
|---|---|
| 前端 | control-panel 浏览器内日志(console.debug + fetch 详情) |
| 入口层 | auth + dispatcher + 共用前置(PII/media) + quota + prompt_compress 内联 |
| cache | L1/L2/L3 缓存查找/写入/淘汰 |
| bridge | LiteLLM 出口 + 熔断 + auto 模型解析 |
| 插件层总开关 | 开启后,只有单独也开的插件才打 debug 日志 |

每个开关下带一行小字说明。写入走 `/admin/config/hot_reload`(扩展现有接口)。

### 3.3 /logs 页 trace 详情弹窗(`Logs.tsx`)

现 `Logs.tsx:401,428-459` 显示 `plugin_trace` 波形图。改为:

- 读取 `/admin/trace/{trace_id}` 返回的 `events` 数组
- 瀑布图:横轴时间,每个 event 一行,按 `stage` 着色(kind=stage 蓝色 / plugin 绿色 / debug 橙色)
- 点击某个 event 展开 `payload`(仅 debug 事件有)
- 旧的 plugin_trace 波形图作为兼容视图保留一个 tab(过渡期),新视图为主

### 3.4 后端接口配套

- `/admin/plugins`(`admin_routes.py`):返回的每个 plugin 增加 `debug` 字段(从 `DebugConfig.per_plugin` 读)
- 新增 `POST /admin/plugins/{name}/debug` `{enabled: bool}` —— 写 `DebugConfig.per_plugin[name]`,触发 config 热重载
- `/admin/config/hot_reload` 扩展:支持读写 `debug:` 段(5 大区开关 + plugins.enabled + per_plugin)
- 新增 `GET /admin/config/debug` —— 返回当前 `DebugConfig`,供前端 control-panel 启动时读 `frontend` 维度

---

## 实现分 3 个 PR(用户确认)

- **PR1**:trace_id 全链路 + TraceEvent 通道(第 1 部分)。包含 model_router 退役(2.5)。prompt_compress **不挪**(保留 dispatcher 内联,见 2.4),其内联埋点直接迁到 `emit(kind="stage", stage="compress")`。PR1 落地后 trace 已正确,可独立验证。
- **PR2**:5 维度 Debug 开关(第 2 部分 2.1-2.3, 2.6)+ 后端接口(3.4)。在 PR1 的 TraceEvent 通道上加 `kind=debug` 事件 + payload 控制。
- **PR3**:控制台 UI(第 3 部分 3.1-3.3)。前端切到新 `/admin/trace` events 接口;**切完后在 PR3 内删除旧 `plugin_trace` 字段及双写逻辑**(1.7 迁移期结束)。

## 不在本次范围

- 不引入 OpenTelemetry collector / Jaeger / Tempo(用户明确拒绝)
- 不改 L1/L2/L3 cache 底层逻辑、LiteLLMBridge 路由、auth 配额、classify_request 分流
- 不动 `media_optimization` / `generation_optimization` 的业务逻辑,只改它们的埋点和 debug 钩子
- `GenerationPipeline`(`media/generation.py`)deprecated 孤儿代码不动(本次不清理)
- 前端 `frontend` debug 维度只控制浏览器内日志,不做前端性能埋点

## 风险与回滚

- **trace_id 必传** 改动可能漏掉某个 ctx 构造点 → 用 grep 全量扫描 `PipelineContext(` 确认,测试覆盖共用前置 + 双管道 + 流式路径
- **Redis 新 key** 增加写入量 → TTL 7 天 + 只在请求结束时一次 flush(非每事件写),影响可控
- **prompt_compress 保留内联** 意味着它的埋点是 `kind="stage"` 而非 `kind="plugin"`,trace 瀑布图里它和 cache/quota 同色(蓝色),非插件绿色 → UI 上需在说明里标注"内联阶段",避免用户困惑为何它没有 Debug 按钮
- **删 debug_mode** 可能影响现有部署脚本/文档 → grep `.env*` / docs 确认无引用,INSTALL.md 同步更新
- 回滚:每个 PR 独立,PR1 出问题回滚后 trace 恢复旧行为(多 trace_id 但不崩);PR2 回滚后 debug 开关失效但 trace 仍工作;PR3 纯前端回滚无风险

## 验证

- 单元测试:`TraceCollector` 累积/flush、`DebugConfig.is_plugin_debug` AND 逻辑、`prompt_compress` 内联埋点迁到 `emit(kind="stage")` 后顺序正确
- 集成测试:发一次 `/v1/chat/completions` 请求,断言 `/admin/trace/{id}` 返回的 events 数组包含 auth→pii→cache→(rag/conv)→bridge 完整链路,且所有事件 trace_id 一致
- debug 测试:开 `prompt_cache` debug,发请求,断言 events 里有 `kind=debug, stage=prompt_cache` 且 payload 非空;关掉后 payload=None
- 前端:开 `frontend` debug,浏览器 console 看到 fetch 详情;关掉后静默
