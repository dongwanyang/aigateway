# 意图驱动路由 + 生成调用路径 设计

**日期**: 2026-07-14
**状态**: 设计稿，待实现
**作者**: brainstorming 会话产出

## 背景与问题

当前 `classify_request`（`aigateway-core/src/aigateway_core/dispatch/classifier.py`）按三条规则把请求分到 understanding / generation 管道：

1. `generation_intent=True`（自定义字段）
2. 多模态 content（messages 含 image/audio/video block）
3. 模型名推断（body.model 命中 providers 里标 generative 模态的模型）

**这条链路有根本缺陷**：路由看的是"模态 + 模型名"，不是"用户意图"。后果：

- **纯文本生成意图被误判**：用户发"帮我画一只猫"（纯文本），指定 `agnes-2.0-flash`（标 `mllm`）。`mllm` 不在判定集 `{generative,image,video}` → 走 understanding 管道。错。用户要画图，却进了理解管道跑 RAG/压缩。
- **带图请求一刀切**：带图请求无条件判 generation。但"这图里是什么"（图生文，理解）和"按这风格再画一张"（图生图，生成）走同一条管道，错一半。
- **bug 复合**：classifier 输出的 `pipeline_kind` 不只路由管道，还决定 bridge 的 `required_modality`（`litellm_bridge.py:99`），即 `auto` 模型解析的候选池。分类错 → 选错模型池 → 文本模型去画图，彻底失败。
- **`generation_intent` 形同虚设**：Claude Code / 普通 OpenAI SDK 不会发这个自定义字段，最高优先级的路径实际是死的。
- **图片/视频生成模型调不通**：`agnes-image-2.1-flash` 用 Images API（`/v1/images/generations`，参数 `prompt`/`size`），`agnes-video-v2.0` 用 `/videos`。但 bridge 的 `completion()` 只发 chat completions 语义（`messages`），没有 Images/Video API 调用路径。即使路由分对、auto 选对模型，bridge 也用错的格式打 endpoint。

OpenAI API 文档（https://developers.openai.com/api/reference/responses/overview 下各分链接）确认：
- 图片生成 `POST /v1/images/generations`：请求体用 `model`/`prompt`/`n`/`size`/`quality`/`response_format`/`output_format`/`style` 等；响应 `{created, data:[{url|b64_json, revised_prompt}], usage}`。`response_format` 是请求体顶级参数（仅 dall-e 系列支持，默认 `url`）。
- 视频生成 `POST /v1/videos`：异步，立即返回 `{id, status:"queued", progress, ...}`，客户端轮询 `GET /v1/videos/{id}` 直到 `status` 变 `completed`/`failed`。
- **本设计严格遵循 OpenAI 权威格式**（非 Agnes 专属格式）。

## 目标

让"帮我画一只猫"真正能画出图。具体：

1. 路由由**用户意图**决定，不是模态/模型名。
2. 图片生成、视频生成能真正调通（用对的 endpoint 和请求格式）。
3. 模型配置简化：多态模型可标，不再用互斥的 mllm/llm/image/video 五类标签。
4. 客户端体验不变：仍发标准 OpenAI chat completions body，网关内部转换。
5. 所有调用大模型的方式都用异步，不阻塞其他任务。

## 非目标

- 不改 understanding 管道本身的插件链（RAG/压缩/会话压缩保留）。
- 不改 PII / media_optimization 共用前置。
- 不改配额/缓存回填逻辑（generation 管道仍不查/回填 prompt cache）。
- 不做图片/视频生成结果的缓存（生成结果缓存语义复杂，留待后续）。

## 设计

### 决策总览

| # | 决策 | 详情 |
|---|------|------|
| 1 | 取消 `generation_intent` 字段 | 意图完全交给 LLM 预判 |
| 2 | 取消模型名推断 | classifier 不再看 `body.model` 决定管道 |
| 3 | 取消 `model=='auto'` 魔法字符串 | 彻底移除，不再保留向后兼容 |
| 4 | 模型标 `capabilities` 多选 | 不再 mllm/llm/image/video 互斥分类 |
| 5 | 撤销每模型自定义 `base_url` | provider 一个 base_url，bridge 按意图自动拼 endpoint 路径 |
| 6 | 意图判定用轻量 LLM 预判 | 最高优先级，带图也看意图不设默认，超时降级 |
| 7 | bridge 增加 Images/Video API 调用分支 | 识别 generation:image/video 意图 → 改用对应格式 |
| 8 | 图片生成 prompt 用 ai_director 改写后的结构化 prompt | 复用 ai_director 输出 |
| 9 | 响应归一 | OpenAI Images API 响应 → chat completions 格式返回给客户端 |
| 10 | 视频生成异步返回任务 ID | 提交任务 → 立即返回 ID，客户端轮询取结果 |
| 11 | 意图预判 + ai_director 用低成本文本模型 | 显式指定模型，不触发智能路由 |
| 12 | 预判输出含 model hint | LLM 同时判断用户是否指定了特定模型 |

### 模型分类层次

**模型层（config 标记能力，可多选）：**

```yaml
providers:
  agnes:
    base_url: https://apihub.agnes-ai.com/v1
    model_grouper:
    - models:
      - name: agnes-2.0-flash
        capabilities: [text, image, video]   # 多态模型
      - name: agnes-image-2.1-flash
        capabilities: [image]                # 专做图片
      - name: agnes-video-v2.0
        capabilities: [video]                # 专做视频
  deepseek:
    base_url: https://api.deepseek.com/v1
    model_grouper:
    - models:
      - name: deepseek-v4-flash
        capabilities: [text]                 # 纯文本推理
```

`capabilities` 是多选数组，取值 `text` / `image` / `video`。一个模型可同时具备多种能力（多态）。

**请求层（意图预判决定这次走哪个能力）：**

意图预判输出固定 JSON 体（字段名固定为 `generation` 和 `hint`）：
```json
{"generation": "image", "hint": "None"}
{"generation": "image", "hint": "deepseek"}
{"generation": "video", "hint": "None"}
{"generation": "understanding", "hint": "None"}
```

- `generation` 字段：`understanding` / `image` / `video`（用户意图类型）
- `hint` 字段：用户明确要求使用的模型名，或 `"None"`（未指定）

意图预判输出决定路由管道（内部映射 `generation` 值 → pipeline_kind）：
- `understanding` —— 走 `/chat/completions`，从 capabilities 含 `text` 的模型池选
- `image` —— 走 `/images/generations`，从 capabilities 含 `image` 的模型池选（pipeline_kind = `generation:image`）
- `video` —— 走 `/videos`，从 capabilities 含 `video` 的模型池选（pipeline_kind = `generation:video`）

**为什么这样分**：endpoint 路径由"这次请求要做什么"决定，不由模型固定标记决定。多态模型 `agnes-2.0-flash` 在"画一只猫"时被选（含 image 能力）打 `/images/generations`；在"解释代码"时被选（含 text 能力）打 `/chat/completions`。同一模型，不同请求，不同路径，都对。

### classifier 改造

`classify_request` 的新优先级：

1. **轻量 LLM 意图预判**（新增，最高优先级）—— 调低成本文本模型，输入 messages，输出 `generation` + `hint` 固定 JSON 体。
2. **超时/失败降级**（兜底）—— 退回老的"带图→generation，纯文本→understanding"启发式。
3. 默认 `understanding`。

**取消的逻辑**：
- 取消 `generation_intent` 检查（决策 1）。
- 取消模型名推断（决策 2）—— classifier 不再读 `body.model`，不再遍历 providers 查 modalities。
- 取消 `_content_modality_hint` 的无条件 generation 判定 —— 带图也交给 LLM 预判，只在降级时用。

**意图预判的实现**：
- 新增 `IntentClassifier` 类（放 `aigateway-core/src/aigateway_core/dispatch/intent_classifier.py`）。
- **异步调用**：使用 `asyncio.wait_for` 调用 `litellm_bridge.completion()`，不阻塞其他请求。
- **模型选择**：先通过智能算法（如连接池健康度、延迟、成本）选出当前最佳连接的廉价文本模型，显式指定给预判调用，避免触发智能路由形成"预判→路由→预判"死循环。
- **预判 prompt**：system 指示"判断用户意图是文本理解/图片生成/视频生成，并判断用户是否指定了特定模型。只输出固定 JSON：`{"generation":"...","hint":"..."}`"；user 放最后一条 user message 文本。
- 超时（默认 3s）/异常 → 降级到启发式（带图→generation:image，纯文本→understanding）。
- 预判结果记入 trace，便于观测。

**降级启发式**：
- 超时/失败时，不再使用旧的"带图→generation"一刀切逻辑，而是：
  - 有 image/audio/video 多模态 content → `generation:image`
  - 纯文本 → `understanding`

### 智能路由改造（取消 auto 魔法字符串）

**现状**：`body.model=='auto'` 时 bridge 调 `_resolve_auto` 按候选池选模型；其他值直连该模型。

**新逻辑**（决策 3：彻底取消 auto，客户端 model 作 hint）：

bridge 的 `completion()` 总是走模型选择逻辑：
- 候选池 = capabilities 含本次意图对应能力（`text`/`image`/`video`）的所有模型。
- **model_hint 来源**：
  1. 客户端传的 `body.model`（如有）
  2. 意图预判返回的 `hint` 字段（如用户明确要求了特定模型）
  3. 两者都有时，取意图预判的 `hint` 优先（用户显式指定 > 客户端默认传参）
- **hint 匹配规则**：如果 hint 在候选池内 → 优先选它（同类内优先）。如果不在候选池内（比如 hint 是 text 模型但意图是 image）→ 忽略 hint，正常选池内模型。
- 客户端没传 model 且预判无 hint → 正常按复杂度/能力评分选。

`intent_evaluator` 复杂度评分保留，用于在候选池内选具体模型（高复杂度选高 capability 模型）。`model_capabilities` 配置保留（用于复杂度选模型），但模态分类从"五类互斥"改为读 `capabilities`。

### bridge 生成调用路径（新增）

bridge 新增按意图分发的调用分支：

```
completion(messages, model_hint, intent, ...)
  ├─ intent == understanding          → _do_completion (chat/completions，现有)
  ├─ intent == generation:image       → _do_image_generation (images/generations，新增)
  └─ intent == generation:video       → _do_video_generation (videos，新增，异步)
```

**endpoint 路径自动拼**（决策 5）：
- 取每模型的 `base_url`（继承自 provider 级，不再每模型配）。
- understanding → `{base_url}/chat/completions`
- generation:image → `{base_url}/images/generations`
- generation:video → `{base_url}/videos`

**`_do_image_generation`**（新增，异步）：
- 从 ai_director 改写后的结构化 prompt 抽取纯文本作 `prompt` 参数（决策 8）。
- **严格遵循 OpenAI Images API 格式**（`POST /v1/images/generations`）。请求体参数（OpenAI 标准，全部顶级，不放 extra_body）：
  - `model`（string）—— 选定的图片模型
  - `prompt`（string, required）—— ai_director 改写后的纯文本
  - `n`（number, 默认 1）—— 生成数量
  - `size`（string, 默认 `1024x1024`）—— 取值如 `1024x1024`/`1792x1024`/`1024x1792`（按模型支持），如用户 message 含显式尺寸词可解析覆盖
  - `quality`（string, 默认 `auto`）—— `standard`/`hd`（dall-e）或 `low`/`medium`/`high`（GPT image）
  - `response_format`（string, 默认 `url`）—— `url`/`b64_json`（仅 dall-e 系列支持；GPT image 模型不支持，默认返回 b64_json）
  - `output_format`（string, 可选）—— `png`/`jpeg`/`webp`（GPT image 模型）
- 调用返回 OpenAI Images API 响应格式：
  ```json
  {"created": 1234567890, "data": [{"url": "https://..."}], "usage": {...}}
  ```
  或 `{"data": [{"b64_json": "..."}]}`（GPT image 模型）。
- **响应归一**（决策 9）：转成 chat completions 格式返回给客户端：
  ```json
  {"choices": [{"message": {"role": "assistant", "content": "<image url>"}, "finish_reason": "stop"}],
   "usage": {"prompt_tokens": N, "completion_tokens": 0, "total_tokens": N}}
  ```
  content 里放图片 URL（纯 URL，初版）。

**`_do_video_generation`**（新增，异步）：
- **严格遵循 OpenAI Videos API 格式**（`POST /v1/videos`，异步）。请求体参数（OpenAI 标准）：
  - `model`（string, 默认按池选）—— 选定的视频模型
  - `prompt`（string, required）—— ai_director 改写后的纯文本
  - `seconds`（string, 默认 `4`）—— `4`/`8`/`12`，片长
  - `size`（string, 默认 `720x1280`）—— `720x1280`/`1280x720`/`1024x1792`/`1792x1024`
  - `input_reference`（object, 可选）—— 参考图，含 `file_id` 或 `image_url`
- 提交任务 → OpenAI 返回 Video 对象，含 `id` 和 `status:"queued"`：
  ```json
  {"id": "video_xxx", "object": "video", "status": "queued", "progress": 0, "created_at": ..., "model": "...", "prompt": "...", "seconds": "4", "size": "720x1280"}
  ```
- bridge 立即返回 chat completions 格式，content 里放 task_id 和轮询端点：
  ```json
  {"choices": [{"message": {"role": "assistant", "content": "Video generation submitted. id=video_xxx, poll /v1/videos/{id}"}}]}
  ```
- 新增 `GET /v1/videos/{id}` 轮询 endpoint（对应 OpenAI 的 Retrieve a video），返回 Video 对象的 `status`/`progress`/`completed_at`/`error` 等。客户端轮询直到 `status` 变 `completed`/`failed`。
- 视频模型从 config 的 generation 候选池正常注册（本次完整实现，不做框架 TODO）。

### dispatcher 改造

- `classify_request` 返回值从 `"understanding" | "generation"` 改为 `"understanding" | "generation:image" | "generation:video"`。
- `_dispatch_generation` 接收带媒介的 pipeline_kind，传给 bridge。
- understanding 管道不变。
- generation 管道：engine 插件链（ai_director 等）跑完后再调 bridge 的对应生成分支。

### 循环依赖的解除

意图预判和 ai_director 都要调 bridge.completion，而 bridge.completion 内部要做智能路由。为避免"预判→bridge→智能路由→又调预判"死循环：

- **意图预判调用 bridge**：先通过智能算法（连接池健康度、延迟、成本）选出当前最佳连接的廉价文本模型，显式传 `model=<选出的模型>`。bridge 见到显式 model + intent="understanding" → 直接走 `_do_completion`（chat/completions），不再触发智能路由，不再调预判。
- **ai_director 调用 bridge**：同样显式传文本模型，走 chat/completions。
- **用户真实请求调用 bridge**：传 `model_hint=body.model`（具体名），`intent=<预判结果>` → 走智能路由 + 对应生成分支。

即：**只有"用户真实请求"才触发智能路由和意图分支；预判/改写这些内部调用固定走文本 chat completions。** 这条边界在 bridge.completion 的参数里明确（`intent` + `model_hint` vs 显式 `model`）。

### 配置模型选择智能算法

在 `IntentClassifier` 中，调用 bridge 前需要通过智能算法查看当前哪个模型是连接健康的，然后显式传 `model=<选出的模型>`。

智能算法（放 `aigateway-core/src/aigateway_core/route/model_resolution/model_selector.py`）：
- 读取所有 capabilities 含 `text` 的模型
- 根据以下条件排序选择：
  1. 连接池健康度（成功率、最近失败次数）
  2. 平均延迟（越低越好）
  3. 成本（prompt + completion 单价）
  4. 配置中的 `model_capabilities` 评分
- 超时（默认 500ms）→ 降级到 config 默认模型

这个智能算法也可被其他地方复用（如意图预判、ai_director 内部调用）。

### config 改造

```yaml
# 新增
intent_classifier:
  model: agnes-2.0-flash        # 智能算法选不出时的默认模型
  timeout_seconds: 3
  fallback: heuristic           # 超时降级策略

model_selector:                   # 新增：配置模型选择智能算法
  health_check_interval: 60
  latency_weight: 0.4
  cost_weight: 0.2
  success_rate_weight: 0.4

generation:
  image:
    default_size: "1024x1024"
    response_format: "url"
    quality: "standard"
  video:
    poll_endpoint: /v1/videos/{task_id}

# providers 模型改标 capabilities
providers:
  agnes:
    base_url: https://apihub.agnes-ai.com/v1
    model_grouper:
    - models:
      - name: agnes-2.0-flash
        capabilities: [text, image, video]
      - name: agnes-image-2.1-flash
        capabilities: [image]
        # base_url 删除（决策 5）
      - name: agnes-video-v2.0
        capabilities: [video]
        # base_url 删除

# 删除
# - ChatCompletionRequest.generation_intent 字段
# - config.yaml plugins: - name: model_router（死配置，CLAUDE.md 已记其插件已删除）
# - generation_optimization.model_router.model_modalities（被 capabilities 取代）
# - model=='auto' 魔法字符串处理
```

`config.yaml.template` 同步更新 schema。

### 数据流（画一只猫，完整链路）

```
客户端: POST /v1/chat/completions
  {"model": "agnes-2.0-flash", "messages": [{"role":"user","content":"帮我画一只猫,赛博朋克风格"}]}

1. dispatcher 共用前置: media_optimization (无多模态,跳过) → PII (无命中)
2. classifier 意图预判:
   - 智能算法选出当前最佳连接模型 agnes-2.0-flash
   - 调 bridge.completion(model="agnes-2.0-flash", messages=<预判prompt>, intent="understanding")
   - LLM 返回 {"generation":"image","hint":"None"}
3. pipeline_kind = "generation:image"
4. _dispatch_generation:
   - engine 跑 ai_director → 改写 prompt 为【主体】猫...【环境】赛博朋克...
   - 调 bridge.completion(messages=..., model_hint="agnes-2.0-flash", intent="generation:image")
5. bridge 智能路由:
   - 候选池 = capabilities 含 image 的模型 [agnes-2.0-flash, agnes-image-2.1-flash]
   - model_hint "agnes-2.0-flash" 在池内 → 优先选它
   - intent="generation:image" → 调 _do_image_generation
6. _do_image_generation:
   - endpoint = https://apihub.agnes-ai.com/v1/images/generations
   - body = {"model":"agnes-2.0-flash","prompt":"【主体】猫...","size":"1024x1024",
            "n":1,"response_format":"url","quality":"auto"}
   - 返回 {"created":..., "data":[{url:"https://..."}], "usage":{...}}
7. 响应归一 → chat completions 格式返回客户端
   {"choices":[{"message":{"content":"https://..."}}]}
```

### 数据流（用户指定特定模型）

```
客户端: POST /v1/chat/completions
  {"model": "deepseek-v4-flash", "messages": [{"role":"user","content":"用 deepseek 帮我画一只猫"}]}

1. classifier 意图预判:
   - LLM 返回 {"generation":"image","hint":"deepseek-v4-flash"}
2. pipeline_kind = "generation:image"
3. bridge 智能路由:
   - 候选池 = capabilities 含 image 的模型 [agnes-2.0-flash, agnes-image-2.1-flash]
   - hint "deepseek-v4-flash" 不在池内（deepseek 只有 text 能力）→ 忽略 hint
   - 按复杂度评分选池内最佳模型
   - intent="generation:image" → 调 _do_image_generation
```

### 错误处理

- **意图预判超时/失败**：降级到启发式（带图→generation:image，纯文本→understanding），记 warning。
- **图片生成 endpoint 返回错误**：按现有 bridge 错误处理（返回 `{error:...}`，status 502）。
- **候选池为空**（某意图无对应能力模型）：返回 `{error: {code: "no_model_for_intent", message: "..."}}`，status 404。
- **模型选择智能算法超时**：降级到 config 默认模型。

### 测试

**新建测试**：
- `tests/test_intent_classifier.py`：预判正确性（画图/视频/理解三类）、超时降级、异常降级、JSON 格式解析、hint 提取。
- `tests/test_generation_routing.py`：capabilities 池过滤、model_hint 优先、hint 不在池内忽略、意图预判 hint 优先于 body.model。
- `tests/test_image_generation.py`：endpoint 路径拼接、prompt 提取、响应归一、OpenAI Images API 格式（请求/响应）。
- `tests/test_video_async.py`：任务 ID 返回、轮询 endpoint 完整流程。
- `tests/test_model_selector.py`：智能算法选模型（健康度/延迟/成本排序）、超时降级到默认模型。

**修改现有测试**：
- `tests/test_cache_key_v2.py`：pipeline_kind 从 `"generation"` 变为 `"generation:image"` / `"generation:video"`，cache key 需适配。
- `tests/test_litellm_bridge.py`：新增 `_do_image_generation` / `_do_video_generation` 测试；移除 `model=='auto'` 分支测试。
- `tests/test_model_router_strategy.py`：`model_modalities` 配置改为 `capabilities` 读取。
- `tests/test_ai_director_strategy.py`：ai_director 调 bridge 时显式传文本模型，不触发智能路由。
- `tests/test_runtime_skeleton_generation.py`：验证 generation 管道 pipeline_kind 带媒介后端到端正常。

**复用现有测试**：
- `tests/test_pii_detector.py`、`tests/test_media_optimization.py`：共用前置不变，无需修改。
- `tests/test_cache_manager.py`：L1/L2 缓存逻辑不变。

## 影响面

**改动文件**：
- `aigateway-core/src/aigateway_core/dispatch/classifier.py`（重写）
- `aigateway-core/src/aigateway_core/dispatch/intent_classifier.py`（新增）
- `aigateway-core/src/aigateway_core/dispatch/dispatcher.py`（pipeline_kind 带媒介）
- `aigateway-core/src/aigateway_core/route/bridge/litellm_bridge.py`（智能路由 + 生成分支 + 取消 auto）
- `aigateway-core/src/aigateway_core/route/model_resolution/model_router.py`（capabilities 池 + 取消 modalities）
- `aigateway-core/src/aigateway_core/route/model_resolution/model_selector.py`（新增：智能模型选择算法）
- `aigateway-api/src/aigateway_api/openai_compat.py`（删 generation_intent 字段）
- `config.yaml` + `config.yaml.template`（capabilities、intent_classifier、删 model_router 死配置、加 model_selector）
- 新增视频轮询 route

**兼容性**：
- 客户端 body 不变（仍发标准 chat completions）。
- `generation_intent` 字段删除：无客户端发它，无影响。
- `model=='auto'` 彻底移除：如有客户端传 auto，会当作普通模型名处理（找不到该模型 → 返回 404），需确认无客户端依赖此行为。
- 模型配置 `modality` 字段改为 `capabilities`：需要迁移现有 config，但配置项在 `config.yaml` 一处，可一次改完。

## 风险

- **每次请求 +1 次意图预判调用**：增加 ~200-500ms 延迟和一次低成本模型调用成本。靠 3s 超时降级控制下限。后续可加缓存（相同 prompt 复用预判结果）。
- **意图预判准确率**：依赖低成本模型理解力。可能误判（"画一只猫"判成 understanding）。靠降级兜底 + trace 观测 + 后续调优 prompt。
- **视频异步语义与 chat 同步的差异**：客户端要改造成轮询（`GET /v1/videos/{id}` 直到 `status` 变 `completed`/`failed`）。本次完整实现视频异步返回任务 ID + 轮询 endpoint（严格遵循 OpenAI Videos API 格式）。
- **OpenAI Images API 格式细节**：`size` 取值按模型支持（dall-e-2: `256x256`/`512x512`/`1024x1024`；dall-e-3: `1024x1024`/`1792x1024`/`1024x1792`；GPT image: `1024x1024`/`1536x1024`/`1024x1536`/`auto`）；`response_format` 仅 dall-e 系列支持（GPT image 模型默认返回 `b64_json`）。初版用 OpenAI 文档记载的标准参数集。
