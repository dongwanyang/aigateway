# AI Gateway 管道/插件/路由/Token 计数缺陷审查报告

审查范围:pipeline、plugins、routing 逻辑、**token 计数与配额**。两条独立审查 agent 的产出 + 主审亲自逐条核实源码(剔除 agent 过度解读)。按严重程度排序,精确到 file:line。**所有 CRITICAL/HIGH 均经主审读源码确认。**

核实修正记录:
- 原缺陷 7(ConvCompressor)agent 报"跨请求 clear/load 竞态",主审核实后改为"同步 LLM 调用阻塞 event loop"——单 worker async + 同步执行段(`clear`→`add`→`load` 之间无 await)下竞态不成立,真实缺陷是同步阻塞。
- 补充 5 条原文件遗漏的 token 计数缺陷(新编号 17-21):流式 video 预扣残留、缓存命中预扣残留(原文件开头"已排除",主审认为应单列)、intent 预判绕过配额、`gateway_cost_total` 用 Gauge。

---

## CRITICAL

### 1. 流式 fallback 把失败 attempt 已输出的内容拼进最终响应
- **file:line**: `aigateway-core/src/aigateway_core/route/bridge/litellm_bridge.py:1304-1338` (`completion_stream`)
- **缺陷描述**: 流式重试时,第一次 attempt 的 chunk 已经 `yield` 给客户端(经 `_wrap_stream_full` 透传到 SSE),若流中途失败,catch 后用同一/下一个 candidate 重试,从头再 `yield` 一遍。
- **失败场景**: 下游 provider 在输出 200 个 token 后连接中断(stream mid-error)→ 200 token 已发给客户端 → 重试成功再发完整 800 token → 客户端收到 "前 200 + 完整 800" 的损坏拼接。且 `_wrap_stream_full` 的 `accum` 累积了所有 attempt 的 content,会写进缓存回填(L1/L2),毒化缓存。`usage` 取 `last_chunk.usage` 也只反映最后一次 attempt。
- **严重程度**: critical

### 2. gen_model_router 的短路错误被 dispatcher 忽略,请求仍发往上游
- **file:line**: `aigateway-core/src/aigateway_core/dispatch/dispatcher.py:510-530` (`_dispatch_generation`) 配合 `aigateway-core/src/aigateway_core/pipelines/generation/routing_signals/gen_model_router_plugin.py:154-181`
- **缺陷描述**: `GenModelRouterPlugin` 在 `ModelRoutingError` 时设 `ctx.mark_stopped()` + `ctx.response = error JSON`。但 `_dispatch_generation` 调 `engine.execute_ctx(ctx)` 后**既不检查 `ctx.should_stop` 也不读 `ctx.response`**,直接继续配额检查 + `_call_llm_nonstream`。(`should_stop` 仅在 understanding 路径的 `_run_engine_filtered:1071` 被检查。)
- **失败场景**: 所有生成模型都不可用 / 复杂度评分缺失 → `gen_model_router` 抛 `ModelRoutingError` 想返回错误 → dispatcher 忽略,继续调 bridge → bridge 返回 `no_model_for_intent` 或 502。用户看到的是 bridge 的错误而非路由层的明确错误,且 `ctx.response` 里构造的错误体被丢弃。
- **严重程度**: critical(短路语义完全失效)

---

## HIGH

### 3. gen_model_router 的复杂度路由决策被整体丢弃,bridge 独立重选 pool[0]
- **file:line**: `dispatcher.py:524-528` + `gen_model_router_plugin.py:132` + `litellm_bridge.py:134-139`
- **缺陷描述**: `gen_model_router` 把 `decision.selected_model` 写入 `ctx.model_router["selected_model"]`,但 dispatcher 只读 `req.get("model")`(即原始 `body.model`,未被 gen_model_router 改写)。因此 6 插件链里 `intent_evaluator`(打分)+`gen_model_router`(按复杂度选模型)的计算结果完全不生效;bridge 的 `_resolve_by_intent` 另起炉灶取 `pool[0]`。
- **失败场景**: 用户发"简单 prompt"应路由到便宜模型,`gen_model_router` 算出 score=10 选了 cheap 模型 → dispatcher 丢弃 → bridge 无视复杂度选 `pool[0]`(可能是最贵的 polymorphic 模型)。cost_tracker 的 `model_routing_saving_usd` 也因此恒为 0(因为 premium_price 兜底回 estimated_cost)。
- **严重程度**: high(整个生成优化层的路由价值被架空)

### 4. 视频生成 token / 成本永不记账(提交和轮询都不记)
- **file:line**: `litellm_bridge.py:846-890` (`_do_video_generation` 返回 `total_tokens:0`) + `aigateway-api/src/aigateway_api/video_routes.py:17-34` (`retrieve_video` 无任何 quota/cost)
- **缺陷描述**: 提交时返回 `usage={total_tokens:0}`,dispatcher 的 `increment_usage(tokens=0)` 把预扣额度完全冲抵为 0。真正的视频生成成本(通常很贵)发生在异步任务里,而 `GET /v1/videos/{id}` 轮询端点不调 `key_store.increment_usage` 也不记 cost/metrics。最终失败也不回退(因为没有"已扣"可言,但也没记任何东西)。
- **失败场景**: 用户提交 100 个视频任务 → 配额系统看到 token=0 → 日/月配额不消耗 → 用户可无限提交视频生成,成本不受任何配额约束;`/metrics` 的 cost 也完全缺失视频部分。
- **严重程度**: high

### 5. 图片生成 usage 恒为 0,配额与成本不消耗
- **file:line**: `litellm_bridge.py:828-844` (`_do_image_generation`) + `dispatcher.py:696-723`
- **缺陷描述**: OpenAI Images API 不返回 token usage,这里 `prompt_tokens` 取自 `payload.usage.input_tokens`,但绝大多数 OpenAI 兼容图片端点不返回 `usage` 字段 → `prompt_tokens=0, total=0`。dispatcher `increment_usage(tokens=0)` 把图片生成预扣完全冲抵;`cost` 也来自 `_meta.cost=0.0`(`completion` 里图片分支硬编码 `cost: 0.0`)。
- **失败场景**: 用户大量生成图片 → 配额 token 不消耗、cost=0 → 月度成本配额形同虚设。应至少按 prompt 字符数估算 prompt_tokens 或按张数计费。
- **严重程度**: high

### 6. AIDirectorStrategy 的 rewrite 调用绕过配额与 gateway metrics
- **file:line**: `pipelines/generation/director/ai_director.py:260-267` + `aigateway-api/src/aigateway_api/main.py:530-541`
- **缺陷描述**: `ai_director` late-bind bridge 后,直接调 `self._litellm_bridge.completion(...)` 做 prompt 改写。这条调用不经过 dispatcher 的 `_call_llm_nonstream`,所以不调 `key_store.increment_usage`、不调 `metrics_collector.record_tokens/record_cost`。只有 LiteLLM 内部 `CostTracker` 看到它(`_track_usage`)。bridge 内部还会走 `_resolve_by_intent` → 可能再触发一次池解析。
- **失败场景**: 每个生成请求先花一次 rewrite 调用(gpt-4o-mini,可能上百 token),这些 token 不进用户配额、不进 Prometheus cost 指标 → 配额可被绕过、成本看板偏低。与已知#1 同型,但这是更贵的整段改写调用。
- **严重程度**: high

### 7. ConvCompressor 同步 LLM 调用阻塞 event loop(原报"竞态",主审修正)
- **file:line**: `pipelines/understanding/conversation/conv_compressor_plugin.py:247, 257-263` + `prefix/registration.py:98`(单例注册)
- **缺陷描述**: `ConvCompressorPlugin` 是全引擎共享单例,`self._memory` 是单个 `ConversationSummaryBufferMemory(llm=ChatOpenAI)` 实例。`_summarize_messages` 先 `self._memory.clear()` 再逐条 `add_user_message/add_ai_message` 再 `load_memory_variables({})`。langchain 的同步 `ChatOpenAI.__call__` 是**纯同步阻塞调用**(不让出 event loop)。
- **主审修正**: agent 原报"两个并发请求在 await 间交错,clear/load 竞态导致 PII 混入"。但单 worker async 下,`clear`→`add_*`→`load_memory_variables` 是同步连续执行(无 await 交错),不会被另一请求的协程打断——**竞态不成立**。真实缺陷是 `load_memory_variables` 内部触发**同步 LLM 摘要调用**,在单 worker async 里阻塞整个 event loop 数秒。
- **失败场景**: 任一请求触发历史压缩 → 同步调 summary_model → event loop 卡死数秒 → 期间所有请求(含 `/health`、流式 chunk 转发)全部阻塞/超时。高并发下表现为 gateway 间歇性无响应。
- **严重程度**: high(可用性,非 PII 泄露)。修复方向:用 langchain async 接口(`aload_memory_variables` / `apredict`),或每请求新建 memory 实例 + 线程池隔离同步调用。

### 8. L3 语义缓存对 group scope 实际退化为 per-user,组内无法共享
- **file:line**: `dispatcher.py:326` (`cache_kwargs = {"user_id": user_id}` 始终传真实 user_id) + `prefix/cache/cache_manager.py:602-607, 271-277` + `shared/qdrant_client.py:243-247`
- **缺陷描述**: `_dispatch_understanding` 无论 `cache_scope` 是 group 还是 private,都把真实 `user_id` 放进 `cache_kwargs`,L3 query 用它做 Qdrant `must` 过滤;L3 backfill (`prefix/cache/l3_semantic.py:158`) 也存真实 user_id。结果:group scope 的 L3 条目只能被同一个 user_id 命中,组内其他成员永远命中不了。
- **失败场景**: 组里用户 A 问了"如何重置密码",L3 存了 `user_id=A`。同组用户 B 问几乎相同的问题,语义相似度 0.97,本应 L3 命中 → 但 Qdrant filter `user_id=B` 过滤掉了 A 的条目 → MISS → 重算 + 重复计费。group scope 的"组内共享"承诺在 L3 层完全失效。(L1/L2 用 hash key 不受影响,但 L3 是语义相似匹配,本应是组共享的主要受益层。)
- **严重程度**: high(命中率与计费双重影响)

---

## MEDIUM

### 9. `_resolve_by_intent` 无 fallback 链:池空只返回错误,fallback_chain 不参与能力过滤
- **file:line**: `litellm_bridge.py:113-139` + `completion` 的 fallback 处理 `623-633`
- **缺陷描述**: `_resolve_by_intent` 只在 `get_registered_models()` 里按能力过滤,`fallback_chain` 参数完全没传给它,也不参与池构建。池空时直接返回 `no_model_for_intent` 错误,不尝试 fallback_chain 中具备能力的模型。另外 `completion` 的 candidates 循环用 `candidates[attempts % len(candidates)]` 在 fallback 链上循环轮转而非"先耗尽主模型再依次 fallback"。
- **失败场景**: 配置了 `agnes-image`(image 能力)+fallback 到 `dall-e`(image 能力),但若主模型 cooldown,池过滤仍含两个 → 选 pool[0]=agnes-image → 失败重试时 `attempts%2` 轮到 dall-e;但若只配了 1 个 image 模型 + 1 个 text fallback,fallback_models 里的 text 模型会被当成 image 模型调用 → `/images/generations` 打到 text 端点报错。
- **严重程度**: medium

### 10. draft_generator 在普通生成请求里同步生成预览,结果被丢弃
- **file:line**: `pipelines/generation/draft/draft_generator_plugin.py:138-159` + `dispatcher.py:524-528`(只读 messages/model)
- **缺陷描述**: `draft_workflow.enabled` 默认 True(`pipelines/generation/_common/config.py:99`)。每个生成请求都会跑 `draft_generator.execute` → `generate_draft` → 探测 ComfyUI + 生成预览(512x512) + 存 Redis。但 dispatcher 从 ctx 只取 `messages`/`model`,previews/draft_id 写进 `ctx.extra["generation_optimization"]["draft_generator"]` 后无人消费;bridge 随后独立调 `_do_image_generation` 再生成一次。
- **失败场景**: ComfyUI 可用时,每个图片生成请求实际触发**两次**图片生成(draft 预览 + bridge 正式生成),双倍 GPU 成本与延迟,且 draft 预览结果永不返回给用户(那是 `draft_routes.py` 的 HITL 路径用的)。普通 `/v1/chat/completions` 生成路径应禁用 draft_workflow 或把 draft 结果作为最终结果返回。
- **严重程度**: medium

### 11. retrieve_video 用"任意一个有 base_url 的 provider"轮询,多 provider 时打错端点
- **file:line**: `litellm_bridge.py:892-910` (`retrieve_video`)
- **缺陷描述**: 提交时 `_do_video_generation` 用 `_get_model_endpoint(model)` 精确定位到模型所属 provider 的 base_url;但轮询 `retrieve_video` 遍历 `providers.values()` 取**第一个**有 base_url 的 provider,与提交时用的 provider 可能不是同一个。
- **失败场景**: provider A(agnes-video)提交了视频任务 → 轮询时 provider B(openai)排在前面 → GET `https://api.openai.com/videos/{id}` → 404 或打到 OpenAI 的 chat 端点 → 用户永远拿不到视频。应按 video_id 记录提交时的 provider/endpoint,或同样用 `_get_model_endpoint`。
- **严重程度**: medium

### 12. PromptCompress 压缩后 messages 结构被破坏(丢弃多轮与 system 之外的角色)
- **file:line**: `pipelines/understanding/compression/plugin.py:100-115` (`_rebuild_messages`) + `117-166`
- **缺陷描述**: 压缩后 `_rebuild_messages` 只保留第一条 system + 把整个压缩文本塞进单条 user message。所有 assistant 历史轮次、tool_calls、多轮 user 全部塌缩成一条 user。这对多轮对话的下游模型理解是毁灭性的(模型看不到对话结构)。
- **失败场景**: 10 轮对话 → LLMLingua-2 压缩 → messages 变成 [system, user(压缩文本)] → 下游模型把整个历史当成一个用户提问 → 答非所问或丢失上下文。压缩"失败"(无收益)时直接 `return ctx` 是 fail-open(正确),但成功路径破坏对话结构。
- **严重程度**: medium

### 13. ConvCompressor 压缩成功时丢工具调用与多模态内容
- **file:line**: `pipelines/understanding/conversation/conv_compressor_plugin.py:145-179, 237-277`
- **缺陷描述**: 压缩失败 → `except` 里只 log,不回写 `ctx.request["messages"]`(fail-open 透传原 messages,正确)。但成功路径 `_summarize_messages` 只取 `msg.get("content")` 字符串,丢弃 tool_calls / function_call / multimodal content;重建的 messages 也没有 tool 角色消息。
- **失败场景**: 含 tool_calls 的对话历史被压缩 → tool_calls 全丢 → 下游模型以为没有待续的工具调用 → 工具调用链断裂。客户端引用第 1 轮内容时,若被摘要成"对话历史摘要",细节丢失 → 答非所问(压缩的固有 trade-off,摘要质量取决于 summary_model)。
- **严重程度**: medium

---

## LOW

### 14. RAG 注入与 cache key(无缺陷,确认正确)
- **file:line**: `pipelines/understanding/rag/rag_retriever_plugin.py:695-722` + `dispatcher.py:305`
- **说明**: cache key 在 `_dispatch_understanding:305` 用 `_extract_cacheable_context(body.messages)` 提前计算,RAG 注入发生在之后的 engine 里,不污染 cache key。L3 backfill 用的是 `normalized_messages`(预 RAG),也正确。这条**无缺陷**。唯一小瑕疵:RAG 注入会修改 `body.messages` 的 system,若该请求 MISS 并回填 L1/L2,L1/L2 存的是含 RAG 上下文的响应——但 key 不含 RAG,所以同 key 下次命中返回的是"上次检索结果对应的回答",若知识库更新了,缓存仍返回旧答案(TTL 内)。这是 RAG+缓存的固有 trade-off,非 bug。

### 15. `_estimate_cost` 用 prompt 单价 × total_tokens,completion 部分被低估
- **file:line**: `litellm_bridge.py:1047-1079`
- **缺陷描述**: `total_tokens * pricing.prompt`(用 prompt 单价当基准),注释说"偏保守估计",但 completion 单价通常远高于 prompt(如 GPT-4o prompt $2.5/M、completion $10/M),用 prompt 价算 total 是**低估**而非保守。
- **失败场景**: 一次请求 prompt=1000、completion=2000 token,真实成本 = 1000×$2.5/M + 2000×$10/M = $0.0225;这里算 3000×$2.5/M = $0.0075,低估 3 倍。影响 LiteLLM CostTracker 与 `_meta.cost`(dispatcher 用它记配额成本)。
- **严重程度**: low

### 16. `_resolve_by_intent` 的 polymorphic 模型选择基本正确,但无 cooldown 过滤
- **file:line**: `litellm_bridge.py:113-139`
- **说明**: polymorphic 模型(如 agnes-2.0-flash 有 text+image+video)通过能力交集正确进入对应池,不会漏选或多选(`required_capability in capabilities`)。但池过滤不考虑 cooldown 状态(`_cooldown_tracker`),pool[0] 若在 cooldown 仍会被选,然后 `completion` 里 `_do_completion` 失败重试才走 fallback。建议在 `_resolve_by_intent` 里排除 OPEN 状态的模型(参照 `route/model_resolution/model_selector.py:88` 的做法)。

---

## HIGH(token 计数专项,补充)

### 17. 流式 video 生成跳过 increment_usage,预扣 token 永久残留
- **file:line**: `dispatch/dispatcher.py:908, 924` + `route/bridge/litellm_bridge.py:1273`(video usage 全 0)
- **缺陷描述**: 流式 video 返回 truthy 但全 0 的 usage dict(`{prompt_tokens:0, completion_tokens:0, total_tokens:0}`)。dispatcher 流式段第 908 行 `if not usage: return` 对 truthy dict 不触发;第 924 行 `if key_hash and key_store and tt > 0` 因 `tt=0` **跳过 `increment_usage`**。而 `check_quota` 在前面已用 `estimated_tokens` 预扣(经 Lua 原子写入 `daily_tokens_used`/`tpm_window_count`)。
- **失败场景**: 每个流式 video 请求预扣 N token(估算)→ `increment_usage` 被跳过 → 预扣的估算 token 永久残留在 `daily_tokens_used`/`tpm_window_count` → 配额被 ghost 消耗,长期积累误报 `quota_exceeded`。video 越多越严重。
- **严重程度**: high。修复方向:video 路径即便 `tt=0` 也要调 `increment_usage` 把 `_reserved_tokens` 冲抵为 0(delta = 0 − reserved)。

### 18. 缓存命中不调 increment_usage,预扣 token 永久残留
- **file:line**: `dispatch/dispatcher.py:368`(check_quota 预扣)→ `:356`→`_handle_cache_hit`(`:987-1034` 不调 increment_usage)
- **缺陷描述**: understanding 路径 `check_quota` 在缓存查找**之前**(第 368 行),预扣 `estimated_tokens`。缓存命中走 `_handle_cache_hit`,该函数只调 `record_tokens_saved` + metrics,**完全不调 `increment_usage`**。预扣的估算 token 永久扣在 `daily_tokens_used`/`tpm_window_count` 上(缓存命中真实消耗应为 0)。
- **失败场景**: 缓存命中率越高的 key,配额被 ghost 消耗越严重。一个高频命中缓存的 key,实际未调任何上游,却因每次预扣残留迅速耗尽日配额。`_handle_cache_hit` 第 987-1034 行确认无 key_store 调用。
- **严重程度**: high。修复方向:`_handle_cache_hit` 末尾调 `increment_usage(tokens=0)` 触发 delta 回填(delta = 0 − reserved,把预扣冲回)。

### 19. intent 预判调用绕过配额与 metrics(预判自身的 token 不计费)
- **file:line**: `dispatch/intent_classifier.py:58-75`(`_do_classify` 直接调 `self._bridge.completion`)
- **缺陷描述**: `IntentClassifier._do_classify` 直接调 `self._bridge.completion(model=text_model, intent="understanding")`,绕过 dispatcher 的 `_call_llm_nonstream`。该调用:① 不调 `key_store.check_quota`/`increment_usage`(预判发生在 dispatcher 配额检查之前),② 不调 `metrics_collector.record_tokens/record_cost`,③ 只有 bridge 内部 `_track_usage`(LiteLLM CostTracker)看到它。
- **失败场景**: 每个请求先花一次预判调用(agnes-2.0-flash,可能上百 token),这些 token 不进用户配额、不进 Prometheus cost 指标 → 配额可被绕过、成本看板偏低。与缺陷 6(AIDirector rewrite)同型,但这是**每个请求**都触发的预判,影响面更广。
- **严重程度**: high。

### 20. quota period 记录 tokens_in/out 被 Lua 和 increment_usage 重复累加(近翻倍)
- **file:line**: `shared/auth/key_store.py:134-136, 181-183`(Lua 预扣写 tokens_in/out) + `:846-860, 951-980`(`_accumulate_quota_record` 再次累加)
- **缺陷描述**: Lua `check_quota` 在预扣阶段已把预估 `tokens` 同时 `HINCRBY` 写入 quota period 的 `tokens_in`/`tokens_out`(第 134-135 行 key、181-182 行 group)和 `cost_usd`(第 136/183 行)。随后 `increment_usage` 又调 `_accumulate_quota_record`(第 951/977 行)把真实 `tokens_in`/`tokens_out`/`cost` **再累加一次**。结果 `aigateway:quota:*:daily/monthly` 里的 `tokens_in`/`tokens_out` ≈ 估算 + 真实(近翻倍)。`request_count` 只加 1(正确)。主计数器 `daily_tokens_used` 走 delta 回填(`_compute_reconciled_updates`),不受影响。
- **失败场景**: 管理后台/`/admin/keys` 展示的"今日已用 tokens_in/out"近翻倍,财务对账对不上。主配额判定(`daily_tokens_used`)正确,但 period 明细失真。
- **严重程度**: high(数据正确性)。修复方向:`_accumulate_quota_record` 改用 delta(真实 − 已在 Lua 预扣的估算),或 Lua 不写 period 的 tokens_in/out、只由 `increment_usage` 写真实值。

### 21. `gateway_cost_total` 用 Gauge 而非 Counter
- **file:line**: `shared/metrics.py:177-181`
- **缺陷描述**: 其他 cost 指标(`gateway_cost_by_model`/`by_user`/`by_group`)都用 Counter,唯独 total 用 Gauge。当前单 worker 功能正确(只 `inc`),但 Gauge 语义上可被 `set` 覆盖,多 worker 下若误用 `set` 会丢累积;且与同族 Counter 不一致,Grafana 查询时易混淆 rate 计算。
- **失败场景**: 未来有人复用该 metric 用 `set` 赋值,或多 worker 下 Counter 自动聚合而 Gauge 行为不一致,导致总成本看板跳变。
- **严重程度**: low(当前无害,架构一致性)。

---

## 关键文件索引
- `/home/ubuntu/aigateway/aigateway-core/src/aigateway_core/route/bridge/litellm_bridge.py` (缺陷 1, 4, 5, 9, 11, 15, 16, 17)
- `/home/ubuntu/aigateway/aigateway-core/src/aigateway_core/dispatch/dispatcher.py` (缺陷 2, 3, 8, 17, 18)
- `/home/ubuntu/aigateway/aigateway-core/src/aigateway_core/dispatch/intent_classifier.py` (缺陷 19)
- `/home/ubuntu/aigateway/aigateway-core/src/aigateway_core/shared/auth/key_store.py` (缺陷 20)
- `/home/ubuntu/aigateway/aigateway-core/src/aigateway_core/shared/metrics.py` (缺陷 21)
- `/home/ubuntu/aigateway/aigateway-core/src/aigateway_core/pipelines/generation/routing_signals/gen_model_router_plugin.py` (缺陷 2, 3)
- `/home/ubuntu/aigateway/aigateway-core/src/aigateway_core/pipelines/generation/director/ai_director.py` (缺陷 6)
- `/home/ubuntu/aigateway/aigateway-core/src/aigateway_core/pipelines/understanding/conversation/conv_compressor_plugin.py` (缺陷 7, 13)
- `/home/ubuntu/aigateway/aigateway-core/src/aigateway_core/pipelines/understanding/compression/plugin.py` (缺陷 12)
- `/home/ubuntu/aigateway/aigateway-core/src/aigateway_core/prefix/cache/cache_manager.py` + `shared/qdrant_client.py` (缺陷 8)
- `/home/ubuntu/aigateway/aigateway-api/src/aigateway_api/video_routes.py` (缺陷 4, 11)

## 优先修复建议
**数据正确性根因(影响所有请求的配额,最该先修):**
- 缺陷 18(缓存命中预扣残留)+ 缺陷 17(流式 video 预扣残留)+ 缺陷 20(quota period 双计)——这三条直接导致配额失真,缓存/视频用得越多越严重。
- 缺陷 19(intent 预判绕过配额)+ 缺陷 6(AIDirector rewrite 绕过配额)——内部调用不计费,配额可被绕过。

**数据完整性:**
- 缺陷 1(流式损坏响应 + 缓存投毒)——已发给客户端的损坏内容 + 毒化缓存。
- 缺陷 4/5(图视频生成完全不计费)——配额可无限绕过。

**功能未接通:**
- 缺陷 2(短路失效)+ 缺陷 3(gen_model_router 决策被丢弃)——整个生成优化层的路由价值被架空。

**可用性:**
- 缺陷 7(ConvCompressor 同步阻塞 event loop)——高并发下 gateway 间歇无响应。
