# AI Gateway 全局架构图

> 生成时间:2026-07-04
> 依据:`openai_compat.py` / `main.py` / `pipeline.py` / `litellm_bridge.py` / `model_router.py` / `.kiro/specs/` 实代码与设计稿核实
> 本文件三部分:
>   - **图 A**:当前运行时真实流程(代码就是这么跑的)
>   - **图 B**:设计稿里的双管道(未实现)
>   - **图 C + 架构建议**:目标「总分总」架构(LiteLLM 作两管道统一出口)及落地建议

---

## 图 A —— 当前运行时真实流程(代码就是这么跑的)

**核心事实**:所有 `/v1/chat/completions` 请求走 `openai_compat.py` 里**一条手工编排的串行链**。
**没有**「理解请求走 A 管道、生成请求走 B 管道」的分流。

证据:
- `PipelineEngine(` 全仓 0 个实例(类定义了,没人 `new`)
- `openai_compat.py` 中 0 处 `engine.execute` / `pipeline_engine`
- `GenerationPipeline` 只在 `media/generation.py:70` 定义,`aigateway-api` 里 0 引用

唯一的分支是 `body.model == "auto"` 时触发 `ModelRouterStrategy` 选模型。

### A.1 请求生命周期总图

```
┌─────────────────────────────────────────────────────────────────────┐
│  客户端 (OpenAI SDK / aigateway CLI / IDE)                           │
│  POST /v1/chat/completions  {model, messages, stream?}              │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  FastAPI (aigateway-api :8000)                                       │
│  main.py lifespan() 在 app.state 上初始化:                          │
│    ConfigManager / KeyStore / CacheManager / LiteLLMBridge /         │
│    PluginRegistry / MediaOptimizationPlugin / ModelRouterStrategy /  │
│    PromptTemplateManager                                             │
│  ⚠ 注:media/pii/compress 插件是单独 new 出来的,不是从               │
│       PluginRegistry 取的;PluginRegistry 装了一堆插件但运行时没被    │
│       PipelineEngine 驱动(因为 PipelineEngine 没有实例)。            │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  auth_middleware.py                                                  │
│  Bearer / x-api-key 校验 → KeyStore 查 key → 写 request.state.user_id│
│  失败 → 401                                                          │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  openai_compat.py                                                    │
│  根据 body.stream 分两条路:                                          │
│    body.stream=False → chat_completions_non_stream()  (:562)        │
│    body.stream=True  → chat_completions_stream()      (:923)        │
│  两条路的处理链结构完全相同,只是最后调用 completion vs completion_stream│
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
                   (进入下面的 A.2 串行处理链)
```

### A.2 串行处理链(理解请求和生成请求走的是同一条链)

```
                   ┌─────────────────────────────────┐
                   │  1. _apply_media_optimization   │  :594 / :956
                   │  仅当 messages 含 list 类型      │
                   │  content(多模态)时才处理:       │
                   │    图片 OCR / 音频转录 / 视频关键帧│
                   │  → 转成文本省 token              │
                   │  无多模态 → 原样透传             │
                   └──────────────┬──────────────────┘
                                  ▼
                   ┌─────────────────────────────────┐
                   │  2. _apply_pii_detection         │  :603 / :965
                   │  PIIDetector 扫 20+ PII 模式     │
                   │  策略: sanitize / reject / hash  │
                   │  reject → 403                    │
                   └──────────────┬──────────────────┘
                                  ▼
                   ┌─────────────────────────────────┐
                   │  3. _resolve_auto_model          │  :625 / :987
  ┌────────────────┤  if body.model != "auto":       │
  │  body.model=   │      原样返回,不路由 ───────────┤──────────┐
  │  "auto"        │  else:                          │          │
  │  (唯一分支)    │    ModelRouterStrategy.route()  │          │
  │                │    模态筛选→能力筛选→最低价      │          │
  │                │    → 选出 model,写回 body.model │          │
  └────────────────┴──────────────┬──────────────────┘          │
                                  ▼                              │
                                  │◄─────────────────────────────┘
                                  ▼
                   ┌─────────────────────────────────┐
                   │  4. 缓存查找 (CacheManager)      │  :647-710
                   │  key = generate_cache_key(       │
                   │    body.model, body.messages)    │
                   │  L1(LRU) → L2(Redis+LZ4) →       │
                   │    L3(Qdrant 向量, cosine≥0.95)  │
                   │  命中 → 200 直接返回(不调 LLM)   │
                   │  MISS → 继续                     │
                   └──────────────┬──────────────────┘
                                  ▼ (MISS)
                   ┌─────────────────────────────────┐
                   │  5. 配额检查 (KeyStore)          │  :727
                   │  日 token / 月 cost /            │
                   │  RPM / TPM 滑动窗口              │
                   │  超限 → 429 quota_exceeded       │
                   └──────────────┬──────────────────┘
                                  ▼
                   ┌─────────────────────────────────┐
                   │  6. _apply_prompt_compression    │  :763 / :1106
                   │  LLMLingua-2 压缩 prompt         │
                   │  (compression_ratio / target_token)│
                   │  按 body.messages 修改           │
                   └──────────────┬──────────────────┘
                                  ▼
                   ┌─────────────────────────────────┐
                   │  7. LiteLLMBridge 调用大模型     │  :786 / :1124
                   │  非流式: litellm_bridge.completion()        │
                   │  流式:   litellm_bridge.completion_stream() │
                   │  → litellm.Router.acompletion     │
                   │  → 真实 provider (OpenAI/Agnes/...)│
                   │  CircuitBreaker 包裹,失败走 fallback │
                   └──────────────┬──────────────────┘
                                  ▼
                   ┌─────────────────────────────────┐
                   │  8. 后处理 + 回填                │  :860-879
                   │  记录 usage / cost → metrics     │
                   │  回填缓存: L1 + L2(+ 异步 L3)    │
                   │  返回响应给客户端                 │
                   └─────────────────────────────────┘
```

### A.3 「理解类 vs 生成类」在当前代码里到底怎么体现?

**答案:基本不体现。** 当前代码不按「理解/生成」分流。两个差异点:

1. **`body.model == "auto"` 分支**(第 3 步):只有显式传 `model: "auto"` 时,`ModelRouterStrategy` 才会根据 `ModelCapability` 筛模型——它的 `route()` 里会按模态(`text`/`image`/`video`/`audio`)和能力(`mllm` 多模态理解 / `generative` 生成)筛。但**这是「选哪个模型」,不是「走哪条管道」**。筛完之后,请求还是回到同一条链的第 4 步继续。

2. **多模态检测**(第 1 步):`messages` 含 `list` 类型 `content`(即带图片/音频)时触发 media optimization。这是「有没有媒体内容」,也不是「理解/生成」。

> 所以你脑子里那个「理解请求和生成请求分别走两条管道」的模型,**在当前跑着的代码里不存在**。所有请求是一条链。理解类请求(纯文本问答)和生成类请求(文生图)的差异,只体现在 `body.model` 这个字符串最终被路由到哪个 provider——而路由逻辑只有 `model=="auto"` 时才跑。

---

## 图 B —— 设计稿里的双管道(未实现,仅存在于 .kiro/specs/)

**核心事实**:以下架构来自 `.kiro/specs/generation-optimization-layer/design.md` 和 `multimodal-gateway-upgrade/design.md`。
**这是设计目标,不是你跑着的系统。** 零件造了大部分,但没装到请求链上。

### B.1 设计意图:两条独立管道

```
                       ┌─────────────────────┐
                       │  请求入口            │
                       └──────────┬──────────┘
                                  │
                  ┌───────────────┴───────────────┐
                  │  请求分析器(设计稿中的分流点)  │
                  │  按意图/模态分流                │
                  └───────┬───────────────┬───────┘
                          │               │
              理解类(mllm)│               │生成类(generative)
              文本/图表/文档理解          文生图/文生视频/TTS
                          │               │
                          ▼               ▼
        ┌─────────────────────────┐ ┌────────────────────────────┐
        │  理解管道 (设计)          │ │  生成管道 (设计)             │
        │  模态: mllm              │ │  模态: generative           │
        │  5 阶段流水线:            │ │  6 插件流水线(优先级序):     │
        │  ① PII 检测               │ │  ① ai_director      (100)  │
        │  ② prompt_cache           │ │     prompt 重写/增强         │
        │  ③ semantic_cache         │ │  ② intent_evaluator (110)  │
        │  ④ model_router           │ │     评估生成请求复杂度       │
        │  ⑤ prompt_compress        │ │  ③ token_compressor (120)  │
        │                           │ │     视觉 token 压缩 + 特征缓存│
        │  → LiteLLM → 理解模型      │ │  ④ draft_generator (130)   │
        │  (VLM / OCR / 文档理解)    │ │     Draft-to-HiRes 草图生成 │
        │                           │ │  ⑤ gen_model_router (140)  │
        │                           │ │     生成感知路由             │
        │                           │ │  ⑥ cost_tracker     (150)  │
        │                           │ │     按 group 聚合成本        │
        │                           │ │  → LiteLLM → 生成模型       │
        │                           │ │  (文生图 / 文生视频 / TTS)   │
        └─────────────────────────┘ └────────────────────────────┘
```

### B.2 设计稿里的模态三分类(来自 design.md:755-756)

```
设计稿定义的模型能力分类:
  "mllm"        = 多模态理解 (VLM / OCR / 图表分析 / 文档理解)
                  输入: 文本+图片/音频/视频 → 输出: 文本
  "generative"  = 生成 (文生图 / 文生视频 / 图生视频 / TTS / 音乐)
                  输入: 文本/图片/音频 → 输出: 图片/视频/音频/3D
```

### B.3 「造好的零件 vs 装上的零件」

| 组件 | 代码里有没有 | 接到请求链上没 |
|---|---|---|
| `PipelineEngine` 类 | ✅ 有定义 (`pipeline.py:60`) | ❌ 0 个实例,没人 new |
| 5 个经典插件 (PII/cache/semantic/router/compress) | ✅ 都注册了 | ⚠ 注册了但运行时由 `openai_compat.py` 手工调用,**不经过 PipelineEngine** |
| `MediaOptimizationPlugin` | ✅ 有 | ✅ 接上了(`_apply_media_optimization`) |
| 6 个生成优化插件 (ai_director 等) | ✅ 都注册了 | ❌ **没接上**,运行时无调用 |
| `ModelRouterStrategy` | ✅ 有真实路由逻辑 | ✅ 接上了(但仅 `model=="auto"` 时) |
| `GenerationPipeline` (Draft-to-HiRes) | ✅ 有定义 (`media/generation.py:70`) | ❌ `aigateway-api` 里 0 引用 |
| 请求分析器(分流理解/生成) | ❌ 设计稿里有 | ❌ 未实现 |

> 一句话:**理解管道的零件基本造好,但用的是「手工串行链」不是「PipelineEngine 驱动」;生成管道的零件造好了,但根本没装到请求链上。**

---

## 图 C —— 建议「总分总」目标架构(LiteLLM 作两管道统一出口)

### 设计原则

用户明确的目标:**总分总结构,LiteLLM 作为两个管道的统一出口**。

```
总(入口分流)  →  分(两条管道各跑插件链)  →  总(LiteLLM 统一出口)
```

### C.1 目标架构图

```
┌──────────────────────────────────────────────────────────────────────┐
│  客户端  POST /v1/chat/completions                                    │
└──────────────────────────────┬───────────────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│  auth_middleware  (不变)                                              │
└──────────────────────────────┬───────────────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│  ▶ 总入口:RequestDispatcher  (★ 新写,当前不存在)                     │
│                                                                        │
│  职责:把每个请求分类为 "understanding" | "generation"                  │
│  分类依据(优先级序):                                                  │
│    1. 显式标记:body 里的 generation_intent / modality 字段(若有)      │
│    2. 模态推断:messages 含 image/audio/video content → 倾向生成/多模态 │
│    3. 模型名推断:body.model 命中 generative 能力模型 → 生成            │
│    4. 默认:understanding                                              │
│  输出:PipelineContext.pipeline_kind = "understanding"|"generation"    │
│  ⚠ 这是整个建议里唯一需要从零写的核心组件                              │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
              ┌────────────────┴────────────────┐
              ▼                                 ▼
┌─────────────────────────────┐   ┌─────────────────────────────────────┐
│  理解管道 (understanding)     │   │  生成管道 (generation)                │
│  PipelineEngine 驱动          │   │  PipelineEngine 驱动                  │
│  mllm / 纯文本理解            │   │  generative: 文生图/视频/TTS          │
│                              │   │                                       │
│  插件链(优先级序):            │   │  插件链(优先级序,来自设计稿):        │
│   ① pii_detector             │   │   ① ai_director        (100)         │
│   ② prompt_cache             │   │      prompt 重写/增强                  │
│   ③ semantic_cache           │   │   ② intent_evaluator   (110)         │
│   ④ model_router             │   │      评估复杂度 → complexity_score    │
│   ⑤ prompt_compress          │   │   ③ token_compressor   (120)         │
│   ⑥ rag_retriever            │   │      视觉 token 压缩 + 特征缓存       │
│   ⑦ conv_compressor          │   │   ④ draft_generator   (130)          │
│                              │   │      Draft-to-HiRes 草图生成          │
│  各插件可独立开关 debug        │   │   ⑤ gen_model_router  (140)          │
│  (本次需求的核心落点)          │   │      生成感知路由                     │
│                              │   │   ⑥ cost_tracker      (150)          │
│                              │   │      按 group 聚合成本                │
│                              │   │                                       │
│                              │   │  各插件可独立开关 debug                │
└──────────────┬──────────────┘   └──────────────────┬──────────────────┘
               │                                       │
               └───────────────────┬───────────────────┘
                                   ▼
┌──────────────────────────────────────────────────────────────────────┐
│  ▶ 总出口:LiteLLMBridge  (★ 已存在,无需新建,仅强化)                  │
│                                                                        │
│  两个管道的请求都汇到这里 → 统一调下游                                   │
│    understanding → completion() / completion_stream()                  │
│    generation    → completion() / completion_stream()                  │
│  (生成类请求若需要多次调用,如 Draft→HiRes,由生成管道内部编排,          │
│   每次单调用仍走此出口)                                                │
│                                                                        │
│  已有能力(沿用):                                                      │
│    - litellm.Router 多 provider 调用                                   │
│    - fallback 链 (主模型失败自动降级)                                  │
│    - CircuitBreaker 包裹                                               │
│    - per-model base_url 覆盖(Agnes 文/图/视频走不同端点)               │
│  建议强化:                                                            │
│    - 配置 litellm 库自身 logger(当前 0 处配置,调试时全是黑盒)          │
│    - fallback 命中时记 info 日志(当前只写 _meta 不记日志)              │
└──────────────────────────────┬───────────────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│  真实模型 (OpenAI / Agnes / ...)                                       │
│    understanding → text/mllm 模型                                      │
│    generation    → generative 模型                                     │
└──────────────────────────────────────────────────────────────────────┘
```

### C.2 与现状的对照(哪些已有、哪些要建)

| 组件 | 现状 | 目标架构里 | 动作 |
|---|---|---|---|
| auth_middleware | ✅ 已有 | 保留 | 无 |
| **RequestDispatcher(分流器)** | ❌ 不存在 | 总入口 | **★ 新写** |
| 理解管道 5 经典插件 | ✅ 已注册,被手工调用 | PipelineEngine 驱动 | 改驱动方式 |
| `PipelineEngine` 类 | ✅ 有定义,0 实例 | 两个管道各一个实例 | new 出来并接入 |
| 生成管道 6 插件 | ✅ 已注册,0 调用 | PipelineEngine 驱动 | 接入请求链 |
| `ModelRouterStrategy` | ✅ 有真实路由 | 两个管道各自的 model_router 内部用 | 复用 |
| `LiteLLMBridge` | ✅ 已是出口 | 总出口(强化) | 仅加日志配置 |
| `MediaOptimizationPlugin` | ✅ 已接入 | 移到分流前(共用) | 调整位置 |
| 缓存层 L1/L2/L3 | ✅ 已有 | 理解管道内(生成管道通常不缓存) | 位置明确 |
| 配额 KeyStore | ✅ 已有 | 分流后、管道内 | 位置明确 |

### C.3 关键设计决策(需你拍板的点)

1. **分流器放哪?**——在 auth 之后、所有处理之前。这样 media_optimization 可以放分流器里(两管道共用),也可以放分流后(各自处理)。我建议**共用前置**:PII 和 media 在分流前跑一遍,两管道都不用重复。
2. **缓存只放理解管道**——生成结果(图片/视频)缓存语义复杂(同一 prompt 出图不同),建议生成管道默认不查缓存,或用单独的媒体缓存(`MediaCacheManager` 已存在)。
3. **debug 开关的统一作用点**——这是本次需求的落点。两个管道都用 `PipelineEngine` 驱动后,每个插件实例上挂一个 `debug_enabled` 属性,`execute()` 入口处按属性门控 `logger.debug()`。全局 debug 按钮(前端+后端+监控)独立于插件 debug,互不耦合。**详见下面的架构建议第 1 条。**

---

## 架构建议(基于图 C,渐进落地)

总判断:**「总分总」方向是对的,LiteLLM 作统一出口这件事现状已经成立**——现在所有请求就经 `LiteLLMBridge.completion()` 出去。真正缺的是「分」那一层:没有分流器,导致理解/生成挤在一条手工链里。所以建议的核心是**补分流层、让 PipelineEngine 真正驱动、再叠 debug 开关**,而不是推倒重来。

### 建议 1:debug 开关(本次需求,优先级最高)

**问题**:现在 5 个经典插件共享一个 logger(`aigateway_core.pipeline`),按 logger 级别开 debug = 一开全开,无法按插件隔离。而你要的是「每个插件一个按钮」。

**方案**:
- 每个插件实例增加 `debug_enabled: bool` 属性(默认 False)。
- 插件 `execute()` 入口处:`if self.debug_enabled: logger.debug(...)` 按插件名门控,不动 logger 级别。
- `PluginRegistration` 增 `debug` 字段;`admin_routes` 的 `PUT /plugins-config` 扩展支持改 `debug`(现在只改 `enabled`)。
- 前端 `Plugins.tsx` 每个插件 Card 加第二个开关(debug),与 enabled 并列。
- **全局 debug 按钮**(已有,`Plugins.tsx:344`)语义明确为:**控制前端 + 后端非插件日志 + 监控**,与插件 debug 无优先级、无范围耦合(这是你之前确认的需求)。
- **「路由层」开关**(独立第三类):门控 `litellm_bridge` + litellm 库自身 + `ModelRouterStrategy` 的日志。这是你之前选的「技术最干净」方案。

**前置依赖**:必须先修热重载(建议 2),否则 debug 开关改了不生效——跟现在 `enabled` 开关一样的坑。

### 建议 2:修热重载(地基,debug 开关的前置)

**问题**:`PUT /admin/global-config` 和 `PUT /admin/plugins-config` 写了 config.yaml 和内存 `_config`,但**不触发 `_notify_reload`**,也不重建已实例化的插件。结果:`debug_mode`、`enabled` 改完要重启才生效。

**方案**:
- PUT 处理器写完文件后,调用 `config_manager._notify_reload()`(或等价的 reload 回调)。
- `main.py` 注册一个 `on_reload` 回调:重建 `PluginRegistry` 里受影响插件的实例(带新的 enabled/debug 配置),替换 `app.state` 上的引用。
- 生成优化层已有 `GenerationOptimizationConfigWatcher` 热重载范式,照搬即可。

### 建议 3:修 contextvar(地基,并发正确性)

**问题**:`logger.py` 的 `ContextInjectProcessor._context` 是类级共享 dict,并发请求互相覆盖 trace_id。

**方案**:改成 `contextvars.ContextVar`,每个请求一个独立上下文。`tracing.py` 已经在用 contextvar 了,对齐即可。这是并发下日志可读性的前提,debug 开关开了之后日志一乱就没法排查。

### 建议 4:落地 RequestDispatcher(总分总的「分」)

**这是工程量最大的一步,也是「总分总」成立的关键。**

- 新写 `RequestDispatcher`(建议放 `aigateway-api/src/aigateway_api/dispatcher.py`)。
- 分类逻辑(C.1 已列):显式标记 → 模态推断 → 模型名推断 → 默认 understanding。
- 分流后,两个管道各用一个 `PipelineEngine` 实例驱动各自的插件链,替换 `openai_compat.py` 现在的手工串行调用。
- **风险点**:`openai_compat.py` 的手工链里有大量边界处理(配额 429、缓存命中短路、5xx 脱敏、stream 包装),迁移到 PipelineEngine 时必须逐个搬进对应插件或 dispatcher,否则会丢行为。建议先写**双跑对比**(新老链同时跑、比对响应),确认无回归后再切换。

### 建议 5:接上生成管道(可选,看产品意图)

- 生成管道 6 插件已注册但 0 调用。接上 = 在 `RequestDispatcher` 判出 `generation` 后,走生成管道的 `PipelineEngine`。
- `Draft-to-HiRes`(draft_generator)需要多次调 LiteLLM 出口,这是理解管道没有的编排逻辑,要在生成管道内部实现。
- **决策点**:这一步是否做,取决于你还想不想要设计稿里的生成优化能力。如果暂时不要,建议把 6 个生成插件从注册表里摘掉(或标记 `enabled=False`),别让休眠插件污染插件管理界面和 debug 开关——给一个永远不被调用的插件配 debug 按钮没意义。

### 建议 6:清理休眠零件(无论是否做建议 5)

- `GenerationPipeline`(`media/generation.py:70`)0 引用——要么接上(建议 5),要么标记废弃。
- `ModelRouterPlugin`(`pipeline.py:585`)是空壳(只复制 model 名),真正路由在 `ModelRouterStrategy`——空壳插件应该删掉,避免和真路由混淆。
- 休眠插件在插件管理界面要么隐藏,要么标灰,要么直接从注册表摘掉。

---

## 落地顺序(每步独立可验证)

```
第 1 步  修 contextvar(建议 3)          ── 并发日志正确性
第 2 步  修热重载(建议 2)              ── 让配置改动生效
第 3 步  debug 开关(建议 1)            ── 本次需求落地 ← 你最初要的
         ↑ 到这里你的需求就满足了,下面是架构演进
第 4 步  清理休眠零件(建议 6)          ── 拿到干净基线
第 5 步  RequestDispatcher + PipelineEngine 驱动(建议 4) ── 总分总的「分」成立
第 6 步  生成管道接入(建议 5,可选)     ── 看产品意图决定
```

**第 1-3 步是你的 debug 开关需求的最小完整闭环**,不动架构、风险低。第 4 步起是架构演进,每步停下系统都可用。

---

## 结论(对照三张图)

1. **图 A(现状)**:单管道手工串行链,理解/生成不分流,LiteLLM 已是事实出口。
2. **图 B(设计稿)**:双管道已设计,零件大多造好,但未接入请求链。
3. **图 C(建议)**:总分总——新写分流器作总入口,两管道由 PipelineEngine 驱动作「分」,LiteLLM 强化作总出口。LiteLLM 作出口这件事现状已成立,真正要补的是分流层。

这份图和建议存盘后,你可以拿它对照代码逐行核实。任何一条箭头/建议你怀疑,我可以给你对应的 `文件:行号`。
