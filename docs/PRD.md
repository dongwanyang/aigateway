# 产品需求文档（PRD）

> 版本: 1.0 | 作者: product-manager | 日期: 2026-06-27
> 基于设计文档: `/home/ubuntu/gateway2/2026-06-27-ai-gateway-design.md` (Status: Approved, rev.2)
> 技术栈: Python + FastAPI, LiteLLM, LangChain, LlamaIndex, Qdrant, Redis, Prometheus + Grafana, React + Vite, OpenTelemetry

---

## 一、项目概述

**产品名称**：AI Gateway

**一句话描述**：一个位于客户端和 LLM 提供商之间的 Token 优化网关，提供提示词压缩、响应缓存、模型路由、工具调用优化和可观测性，作为开箱即用的 OpenAI API 兼容代理。

**核心价值**：以极低成本（零代码修改）接入现有 AI 应用，通过多级缓存、模型路由和安全脱敏显著降低 LLM 调用成本并提升响应速度；支持 API Gateway、CLI、IDE 插件、Docker Compose 四种使用形式，覆盖不同用户场景。

**四个使用形式**：
1. **API Gateway** -- 现有应用的 OpenAI 代理替代方案，设置 `base_url` 和 `api_key` 即可自动接入
2. **CLI Tool** -- `aigateway chat`，终端交互式对话，零代码改动
3. **IDE Extension** -- VS Code / Cursor 插件，自动拦截所有 AI 请求，零配置优化
4. **Docker Compose** -- 一条命令部署全部服务（Gateway + CLI + Dashboard + 基础设施）

---

## 二、目标用户

| 用户类型 | 描述 | 使用场景 | 使用频率 |
|---------|------|---------|---------|
| 后端工程师 | 使用 OpenAI SDK 开发 AI 应用的开发者 | 将 `base_url` 指向 AI Gateway，享受缓存、压缩、路由等能力，无需改代码 | 日/周频次 |
| CLI 用户 | 需要终端快速测试/使用 LLM 的开发者 | 安装后直接 `aigateway chat` 进行对话 | 周/月频次 |
| IDE 用户 | 在 VS Code/Cursor 中使用 AI 助手的开发者 | 安装插件后自动拦截 AI 请求，享受去重、缓存、成本优化 | 日频次 |
| DevOps/SRE | 负责部署和维护 AI 基础设施的团队 | Docker Compose 一键部署，Grafana 监控，控制面板管理配额 | 月频次 |

---

## 三、功能范围

### MVP 范围（本期实现）

| 编号 | 功能名称 | 优先级 | 简述 |
|------|---------|--------|------|
| F01 | API Gateway 代理 | P0 必做 | OpenAI API 兼容层，支持 `/v1/chat/completions`、`/v1/models`、流式响应 |
| F02 | 插件管线引擎 | P0 必做 | 异步插件管线 + 注册表，支持插件依赖声明和启停控制 |
| F03 | 三级缓存系统 | P0 必做 | L1 进程内存(LRU) -> L2 Redis(KV) -> L3 Qdrant(语义)，命中时短路管线 |
| F04 | 模型路由与降级 | P0 必做 | LiteLLM Router，支持 cost/speed/quality 策略、fallback 链、重试 |
| F05 | API Key 认证与配额 | P0 必做 | API Key 校验、per-key 配额管理（日 token/月成本/RPM/TPM）、Redis Pub/Sub 分布式同步 |
| F06 | PII 检测与脱敏 | P0 必做 | 正则匹配，sanitize/reject/hash 三种策略，覆盖 20+ 类敏感信息 |
| F07 | CLI 交互工具 | P1 重要 | `aigateway chat` 交互式对话，`aigateway run` 单次请求，会话管理 |
| F08 | 配置热加载 | P1 重要 | YAML 配置 + 环境变量覆盖，`watchdog` 文件监听，原子交换 |
| F09 | 熔断器 | P1 重要 | 每提供商独立 circuit breaker（CLOSED/OPEN/HALF-OPEN） |
| F10 | 可观测性 | P1 重要 | Prometheus 指标(`/metrics`)、OpenTelemetry 全链路 trace、结构化 JSON 日志 |
| F11 | 控制面板(Dashboard) | P2 建议 | React SPA，展示用量/成本/缓存命中率，支持插件开关和配额管理 |
| F12 | IDE 扩展 | P2 建议 | VS Code/Cursor 插件，拦截指定域名的 AI 请求并转发至本地 Gateway |
| F13 | Docker Compose 集成 | P1 重要 | 一键启动所有服务（Gateway + CLI + Dashboard + Qdrant + Redis + Prometheus + Grafana） |
| F14 | 提示词压缩 | P1 重要 | 压缩输入 token 量，目标 30%+ 削减 |
| F15 | 流式缓存响应 | P1 重要 | 缓存命中时按 SSE 格式分块流式返回（20ms/chunk 延迟模拟真实 LLM） |
| F16 | 插件故障隔离 | P1 重要 | 非关键插件（缓存/压缩/RAG）失败时 fail-open，请求继续到达 LLM |

### 本期不做（明确排除）

| 功能 | 排除原因 |
|------|---------|
| 侧边栏部署(Sidecar/K8s) | 设计文档标记为 v2+，涉及 K8s 编排复杂度 |
| DAG 并行插件调度 | 设计文档标记为 v2+，线性管线满足 MVP，DAG 引入 networkx 复杂度 |
| 异步任务队列(RAG 处理) | 设计文档标记为 v2+，Celery 增加运维负担，MVP 同步处理即可 |
| Feature Flag 平台 | 设计文档标记为 v2+，Unleash 集成超出 MVP 范围 |
| RAG 文档检索 | P2 以下，MVP 仅确保管道预留位置，实际检索引擎延后 |
| MCP 工具调用优化 | 设计文档默认 disabled，MVP 不启用 |
| 对话摘要压缩(Conv Compressor) | 设计文档默认 disabled，MVP 不启用 |
| 响应格式器(Response Formatter) | 设计文档默认 disabled，MVP 不启用 |

---

## 四、用户故事与验收标准

### US01：通过 API Gateway 代理使用 LLM（对应 F01/F04）

**故事**：作为后端工程师，我希望将应用的 `OPENAI_BASE_URL` 指向 AI Gateway，以便在不修改代码的前提下享受缓存、路由和成本优化。

**验收标准**：
- [ ] 场景1：给定已部署 Gateway，当客户端设置 `OPENAI_BASE_URL=http://localhost:8000/v1` 并发送 `/v1/chat/completions` 请求，则 Gateway 正确路由至下游 LLM 并返回标准 OpenAI 响应格式
- [ ] 场景2：给定流式请求(`stream=true`)，当 LLM 返回流式响应，则 Gateway 以 SSE 格式透传 chunk 到客户端
- [ ] 场景3：给定下游 LLM 超时/5xx，当触发 LiteLLM Router fallback 链，则自动切换到备用模型并重试，最终返回有效响应
- [ ] 场景4：给定 `/v1/models` 请求，当发送至 Gateway，则返回配置的提供商模型列表
- [ ] 性能：`/v1/chat/completions` 在无缓存命中场景下端到端响应时间 < 下游 LLM 响应时间 + 50ms（管线开销上限）

### US02：插件管线与短路机制（对应 F02/F03/F15/F16）

**故事**：作为系统架构师，我希望每条请求经过可配置的插件管线处理，并在缓存命中时短路后续插件，以便最大化性能和灵活性。

**验收标准**：
- [ ] 场景1：给定 pipeline 配置了 `prompt_compress -> prompt_cache -> semantic_cache -> model_router`，当缓存命中(L1/L2/L3)，则 `context.should_stop=True` 且跳过剩余插件，直接返回缓存响应
- [ ] 场景2：给定流式缓存命中，当返回缓存响应，则 Gateway 将响应分块并通过 SSE 发送（20ms/chunk 延迟），客户端无法区分缓存命中与真实 LLM
- [ ] 场景3：给定 Redis 服务不可用，当请求经过 prompt_cache 插件，则跳过缓存层继续执行后续插件，记录 WARNING 日志，请求不被中断
- [ ] 场景4：给定 Qdrant 服务不可用，当请求经过 semantic_cache 插件，则跳过语义缓存继续执行后续插件，记录 WARNING 日志
- [ ] 场景5：给定插件声明了 `depends_on`，当启动时依赖插件未启用或顺序错误，则 Gateway 记录 ERROR 日志并跳过该插件
- [ ] 性能：L1 缓存命中延迟 < 1ms，L2 缓存命中延迟 < 5ms，L3 缓存命中延迟 < 50ms

### US03：API Key 认证与配额管理（对应 F05）

**故事**：作为多租户管理员，我希望每个 API Key 拥有独立的配额和限额，以便控制成本并防止滥用。

**验收标准**：
- [ ] 场景1：给定有效 API Key（`Authorization: Bearer` 或 `x-api-key` 头），当请求发往 Gateway，则校验通过并继续处理
- [ ] 场景2：给定无效/过期 API Key，当请求发往 Gateway，则返回 `401 Unauthorized`
- [ ] 场景3：给定 API Key 已达 `daily_token_limit`，当新请求到达，则返回 `429 Too Many Requests` 并附带 `Retry-After` 头
- [ ] 场景4：给定 API Key 已达 `monthly_cost_limit`，当新请求到达，则返回 `429 Too Many Requests`
- [ ] 场景5：给定 usage 达到预算 80%，当阈值触发，则记录 WARNING 并推送到控制面板（软告警，不拦截请求）
- [ ] 场景6：给定多实例部署，当新 API Key 创建或旧 Key 撤销，则通过 Redis Pub/Sub 同步到所有 Gateway 实例

### US04：PII 检测与脱敏（对应 F06）

**故事**：作为安全合规负责人，我希望在请求进入 LLM 前自动检测和脱敏敏感个人信息，以满足隐私合规要求。

**验收标准**：
- [ ] 场景1：给定提示词包含电子邮件地址，当处理管线执行 PII 检测，则替换为 `[EMAIL_REDACTED]` 并记录脱敏类别
- [ ] 场景2：给定提示词包含中国身份证号（18位），当处理管线执行 PII 检测，则替换为 `[CN_ID_REDACTED]`
- [ ] 场景3：给定提示词包含信用卡号（符合 Luhn 验证），当处理管线执行 PII 检测，则替换为 `[CC_REDACTED]`
- [ ] 场景4：给定 PII 策略设为 `reject`，当检测到敏感信息，则返回 `400 Bad Request`，响应体列出检测到的类别
- [ ] 场景5：给定 PII 策略设为 `hash`，当检测到敏感信息，则替换为 `SHA256(mask_token + original_value)`，保证相同输入产生相同哈希值（cache-friendly）
- [ ] 场景6：给定 UUID/版本号/十六进制颜色/ISO 日期/SKU，当执行 PII 检测，则这些模式被排除，不发生误报
- [ ] 场景7：给定提示词 > 50KB，当执行 PII 检测，则仅扫描前 10KB 和后 10KB，其余部分采样跳过
- [ ] 场景8：给定提示词 > 10KB 且 <= 50KB，当执行 PII 检测，则在 ThreadPoolExecutor(max_workers=4) 中异步执行，不阻塞事件循环
- [ ] 性能：提示词 < 1KB 时 PII 检测开销 < 0.5ms；< 10KB 时 < 5ms；< 50KB 时 < 20ms

### US05：CLI 交互式对话（对应 F07）

**故事**：作为开发者，我希望在终端中直接运行 `aigateway chat` 与 LLM 对话，以便快速测试 Prompt 和模型效果。

**验收标准**：
- [ ] 场景1：给定已安装 CLI 包，当执行 `aigateway chat`，则进入交互式输入模式（基于 `rich`/`prompt-toolkit`），等待用户输入并显示 LLM 响应
- [ ] 场景2：给定执行 `aigateway run --prompt "..." --format json`，则向 Gateway 发送单次请求并以指定格式返回结果
- [ ] 场景3：给定执行 `aigateway chat --session my-project`，则使用该会话名存储和恢复上下文历史
- [ ] 场景4：给定 CLI 连接 Gateway 失败，则返回清晰的错误信息（含 `--help` 提示）而非 Python 堆栈跟踪

### US06：配置热加载（对应 F08）

**故事**：作为运维人员，我希望修改 `config.yaml` 后自动生效，不必重启服务。

**验收标准**：
- [ ] 场景1：给定 Gateway 运行中，当修改 `config.yaml` 并保存，则 `watchdog` 检测到变更，加载新配置，新请求使用新配置，活跃请求不受影响
- [ ] 场景2：给定新配置存在语法错误，当 `config.yaml` 被修改为无效 YAML，则拒绝加载并保留上一份有效配置，记录 ERROR 日志
- [ ] 场景3：给定环境变量设置了 `AI_GATEWAY_LOG_LEVEL=debug`，当读取配置，则环境变量优先于 YAML 中的对应值

### US07：熔断器与故障隔离（对应 F09/F16）

**故事**：作为系统运维者，我希望每个下游提供商拥有独立的熔断器，以便在一家供应商不可用时快速降级，不影响其他提供商。

**验收标准**：
- [ ] 场景1：给定 OpenAI 提供商连续失败 5 次，当熔断器状态变为 OPEN，则新请求立即被拒绝并触发 fallback，不等待 LLM 超时
- [ ] 场景2：给定熔断器处于 OPEN 状态超过 `recovery_timeout`(60s)，当进入 HALF-OPEN 态，则放行一个探测请求，成功后回退到 CLOSED
- [ ] 场景3：给定熔断器处于 OPEN 状态，当探测请求失败，则熔断器重新进入 OPEN 状态
- [ ] 场景4：给定 OpenAI 熔断器 OPEN，但 Anthropic 正常，则请求通过 fallback 路由到 Anthropic，不受影响

### US08：可观测性与监控（对应 F10/F11）

**故事**：作为 DevOps/SRE，我希望通过 Prometheus + Grafana + OpenTelemetry 全面监控 Gateway 的运行状态，以便快速定位问题。

**验收标准**：
- [ ] 场景1：给定 Gateway 运行中，当访问 `http://localhost:8000/metrics`，则返回 Prometheus 格式的指标（包含请求数、延迟、缓存命中率、token 消耗、成本等）
- [ ] 场景2：给定请求经过完整管线，当在每个插件阶段注入相同的 `trace_id`，则 Prometheus 指标、OpenTelemetry 追踪、结构化日志共享同一 trace_id
- [ ] 场景3：给定 Grafana 面板加载成本指标，当点击某个高延迟数据点，则可从 Metric 跳转到该 trace_id 对应的 OpenTelemetry 详情页
- [ ] 场景4：给定 OpenTelemetry 采样率配置为 0.1（10%），当统计 trace 数量，则仅有 10% 的请求生成了完整 trace
- [ ] 场景5：给定结构化日志启用，当每条日志包含 `trace_id`、`request_id`、`user_id` 字段，则输出格式为 JSON
- [ ] 场景6：给定控制面板运行中，当访问 Dashboard 页面，则 Overview 页展示 QPS/延迟/成本总览，Costs 页展示各模型费用分布，Cache 页展示各级缓存命中率

### US09：Docker Compose 一键部署（对应 F13）

**故事**：作为运维人员，我希望执行 `docker compose up -d` 即可启动包含 Gateway、Dashboard、所有基础设施的完整环境。

**验收标准**：
- [ ] 场景1：给定干净的 Docker 环境，当执行 `docker compose up -d`，则所有服务（aigateway-api、control-panel、qdrant、redis、prometheus、grafana）成功启动
- [ ] 场景2：给定全部服务启动完成，当访问 `http://localhost:8000/v1/models`，则返回模型列表
- [ ] 场景3：给定全部服务启动完成，当访问 `http://localhost:3000`，则显示控制面板界面
- [ ] 场景4：给定 Grafana 已启动，当访问默认端口（3000），则内置 datasource 已配置指向 Prometheus

---

## 五、非功能性需求

| 类别 | 要求 | 量化指标 |
|------|------|---------|
| 性能 -- 缓存 L1 | 进程内缓存命中延迟 | < 1ms |
| 性能 -- 缓存 L2 | Redis KV 缓存命中延迟 | < 5ms |
| 性能 -- 缓存 L3 | Qdrant 向量缓存命中延迟 | < 50ms |
| 性能 -- PII 检测 | 小提示词(<1KB) 检测开销 | < 0.5ms |
| 性能 -- PII 检测 | 中等提示词(<10KB) 检测开销 | < 5ms |
| 性能 -- PII 检测 | 大提示词(<50KB) 检测开销 | < 20ms |
| 性能 -- 管线总开销 | 无缓存命中时的额外延迟 | 比直接调 LLM < 50ms |
| 缓存命中率 | Prompt 精确缓存命中 | > 50%（重复请求） |
| 缓存命中率 | 语义缓存命中 | > 30%（相似问题） |
| 缓存效率 | 提示词压缩 | 减少输入 token 30%+ |
| 并发 | 单实例支持的并发请求 | [待确认] 500 RPS |
| 可用性 | 服务可用率 | 99.9%（月均不超过 43 分钟宕机） |
| 安全 | API 请求鉴权 | 所有 `/v1/*` 接口需有效 API Key |
| 安全 | PII 防护 | 20+ 类敏感信息覆盖，sanitize/reject/hash 可选 |
| 安全 | 分布式密钥同步 | Redis Pub/Sub 广播，近实时一致性 |
| 兼容性 | 下游 LLM 提供商 | OpenAI / Anthropic / Gemini / Bedrock / Ollama |
| 兼容性 | API 格式 | OpenAI API 兼容（`/v1/chat/completions`, `/v1/models`） |
| 日志 | 结构化输出 | JSON 格式，含 trace_id/request_id/user_id |
| 浏览器 | 控制面板支持 | Chrome/Firefox/Safari 最新两个版本 |
| 可扩展性 | 插件系统 | 第三方插件可通过 BasePlugin 接口注册 |

---

## 六、成功标准（逐条对应设计文档 Section "Success Criteria"）

| 编号 | 成功标准（来自设计文档） | 对应功能 | MVP 状态 | 验收依据 |
|------|------------------------|---------|---------|---------|
| SC01 | API Gateway: Drop-in OpenAI API replacement | F01/F04 | 本期 | US01 |
| SC02 | CLI: `aigateway chat` works out of the box | F07 | 本期 | US05 |
| SC03 | IDE: Extension intercepts AI assistant requests | F12 | 本期 | US01+F12 |
| SC04 | Prompt compression reduces input tokens by 30%+ | F14 | 本期 | F14 验收 |
| SC05 | Prompt cache hit rate > 50% on repeated requests | F03 | 本期 | F03 验收 |
| SC06 | Semantic cache hit rate > 30% on similar questions | F03 | 本期 | F03 验收 |
| SC07 | All plugins independently toggleable via config | F02 | 本期 | US02 |
| SC08 | PipelineContext.extra uses namespaced keys | F02 | 本期 | 代码审查 |
| SC09 | Cache hit short-circuits remaining plugins | F03 | 本期 | US02-场景1 |
| SC10 | Streaming cache hits simulate SSE chunking | F15 | 本期 | US02-场景2 |
| SC11 | API Key auth with per-key quota management | F05 | 本期 | US03 |
| SC12 | API Key changes synced across distributed instances | F05 | 本期 | US03-场景6 |
| SC13 | PII detection with configurable strategy | F06 | 本期 | US04 |
| SC14 | PII detection covers 20+ categories | F06 | 本期 | US04-场景1~8 |
| SC15 | Named-field pass before standalone pattern pass | F06 | 本期 | US04-处理流程验证 |
| SC16 | Exclusion patterns for UUIDs, versions, hex colors, etc. | F06 | 本期 | US04-场景6 |
| SC17 | Mask tokens are deterministic and cache-friendly | F06 | 本期 | US04-场景3(hash) |
| SC18 | PII detection runs async and samples long prompts | F06 | 本期 | US04-场景7~8 |
| SC19 | Estimated PII overhead: <0.5ms/<5ms/<20ms | F06 | 本期 | NFR 表 |
| SC20 | Config hot reload with atomic swap | F08 | 本期 | US06-场景1~2 |
| SC21 | Env var overrides YAML config | F08 | 本期 | US06-场景3 |
| SC22 | Plugin dependency validation at startup | F02 | 本期 | US02-场景5 |
| SC23 | Pipeline short-circuit on cache hits | F03 | 本期 | US02-场景1 |
| SC24 | API Key auth with per-key quota management | F05 | 本期 | 重复 SC11，合并 |
| SC25 | PII detection with configurable strategy | F06 | 本期 | 重复 SC13，合并 |
| SC26 | Model fallback and retry via LiteLLM | F04 | 本期 | US01-场景3 |
| SC27 | Fail-open plugin behavior | F16 | 本期 | US02-场景3~4 |
| SC28 | Prometheus metrics exposed at /metrics | F10 | 本期 | US08-场景1 |
| SC29 | OpenTelemetry trace ID propagated through entire pipeline | F10 | 本期 | US08-场景2 |
| SC30 | OTel configurable sampling rate (default 10%) | F10 | 本期 | US08-场景4 |
| SC31 | Structured JSON logging with trace_id, request_id, user_id | F10 | 本期 | US08-场景5 |
| SC32 | Config hot reload without restart | F08 | 本期 | 重复 SC20，合并 |
| SC33 | Env var overrides YAML config | F08 | 本期 | 重复 SC21，合并 |
| SC34 | Grafana dashboard shows cost, tokens, cache hit rate, latency | F11 | 本期 | US08-场景6 |
| SC35 | docker-compose up brings up all services | F13 | 本期 | US09-场景1 |
| SC36 | Control panel allows runtime plugin toggle, config, and quota management | F11 | 本期 | US08-场景6 |
| SC37 | Soft budget alerts at 80% usage | F05 | 本期 | US03-场景5 |
| SC38 | RAG file lifecycle: upload/update/delete | F01 预留 | v2+ | 见"本期不做"表 |
| SC39 | Multi-level cache: L1->L2->L3 progressive fallback | F03 | 本期 | US02 |
| SC40 | Circuit breaker per provider | F09 | 本期 | US07 |
| SC41 | trace_id attached as Prometheus metric label | F10 | 本期 | US08-场景2/3 |

---

## 七、假设与待确认项

### 假设

- [假设] 下游 LLM 提供商通过 LiteLLM 的标准格式配置，Gateway 仅需转发标准化请求即可
- [假设] IDE 扩展通过 HTTP 代理模式工作（而非 LSP 协议），仅拦截 `fetch`/`XMLHttpRequest` 到已知 AI 域名的请求
- [假设] RAG 文档处理采用同步方式执行（MVP 不用 Celery 异步队列），文件上传后阻塞等待索引完成
- [假设] 控制面板不直接操作数据库，所有数据从 Gateway 的 `/metrics` 和 Admin API 获取
- [假设] 单台 Gateway 实例最低配置为 4 CPU / 8GB RAM，可支撑约 500 RPS
- [假设] PII 正则匹配不使用 ML 模型，纯规则引擎以满足性能约束
- [假设] 三级缓存的 L3 语义缓存 embedding 使用 `sentence-transformers` 本地模型（`all-MiniLM-L6-v2`），以减少对外部 API 的依赖
- [假设] IDE 扩展首次使用时需用户手动填写 Gateway URL，之后自动拦截同域名请求

### 待确认

- [待确认] IDE 扩展是否发布到 VS Code Marketplace，还是仅作为开源项目供本地安装？
- [待确认] 控制面板是否需要用户注册/登录功能，还是仅供内部运维访问？
- [待确认] PII 检测的模式库是否需要支持动态更新（如通过 API 添加新规则），还是 MVP 阶段静态 YAML 配置足够？
- [待确认] 多租户场景中，用户配额管理的审批流程是由人工还是 API 自动化触发？
- [待确认] Grafana 面板是否需要导出为 JSON 文件纳入版本控制，还是直接在 Grafana UI 配置？
- [待确认] 是否需要对特定行业合规（如 GDPR/HIPAA）做额外的 PII 类别覆盖？

---

## 八、名词解释

| 术语 | 定义 |
|------|------|
| AI Gateway | 介于客户端和 LLM 提供商之间的代理层，提供缓存、压缩、路由、安全等能力 |
| PipelineContext | 跨插件传递的共享状态对象，包含 request/response/should_stop/trace_id 等字段 |
| Short-circuit | 当缓存命中时，设置 `should_stop=True` 终止管线执行，直接返回缓存响应 |
| L1/L2/L3 缓存 | 三级缓存：L1=进程内 LRU 缓存(<1ms)，L2=Redis KV 缓存(<5ms)，L3=Qdrant 向量语义缓存(<50ms) |
| Circuit Breaker | 熔断器模式，分 CLOSED/OPEN/HALF-OPEN 三种状态，防止级联故障 |
| PII | Personally Identifiable Information，个人身份信息 |
| Fail-Open | 非关键组件故障时，不阻断主流程（如缓存挂了就直接调 LLM） |
| SSE | Server-Sent Events，服务端推送的流式事件协议 |
| Trace ID | 分布式追踪标识符，贯穿整个请求管线，关联日志/指标/追踪 |
| RAG | Retrieval-Augmented Generation，检索增强生成 |
| MCP | Model Context Protocol，模型上下文协议 |
| OTel | OpenTelemetry，开放可观测性框架 |
