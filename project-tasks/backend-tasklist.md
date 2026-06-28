# 后端任务清单

> 基于 API_CONTRACT v1.0，每任务对应一个接口

## aigateway-core（核心库）

### [x] TASK-C01：PipelineContext 共享状态
- 对应契约：DB_SCHEMA.md #PipelineContext
- 验收标准：`PipelineContext` 包含 request/response/should_stop/should_stream/trace_id/request_id/user_id/extra 字段，extra 使用命名空间约定

### [x] TASK-C02：异步插件管线引擎
- 对应契约：PRD.md #F02
- 验收标准：`PipelineEngine` 按配置顺序执行插件，支持短路（should_stop=True 时跳过后续插件）

### [x] TASK-C03：插件注册表与依赖校验
- 对应契约：PRD.md #F02, API_CONTRACT 插件 depends_on
- 验收标准：启动时校验依赖图，禁用或未放置依赖插件时跳过并记录 ERROR

### [x] TASK-C04：YAML 配置加载器 + 环境变量覆盖 + 热重载
- 对应契约：TECH_SPEC.md #config.yaml, PRD #F08
- 验收标准：环境变量优先于 YAML，watchdog 监听变更，原子交换

### [x] TASK-C05：三级缓存系统（L1/L2/L3）
- 对应契约：DB_SCHEMA.md #缓存键, PRD #F03
- 验收标准：L1 LRUCache（<1ms）→ L2 Redis（<5ms）→ L3 Qdrant（<50ms），命中时设置 should_stop

### [x] TASK-C06：API Key 认证与配额管理器
- 对应契约：API_CONTRACT.md #auth, DB_SCHEMA.md #Redis Key 结构
- 验收标准：验证 Bearer/x-api-key，检查日/月配额和 RPM/TPM 速率限制，返回 401/429

### [x] TASK-C07：PII 检测与脱敏处理器
- 对应契约：设计文档 #PII Pattern Catalog, PRD #F06
- 验收标准：sanitize/reject/hash 三种策略，覆盖 20+ 类敏感信息，排除 UUID/版本/颜色/ISO 日期/SKU

### [x] TASK-C08：LiteLLM 桥接层（Router + Completion + CostTracker）
- 对应契约：API_CONTRACT.md #POST /v1/chat/completions, TECH_SPEC.md #providers
- 验收标准：转发请求到下游 LLM，支持 fallback 链和重试，记录成本

### [x] TASK-C09：熔断器（per-provider）
- 对应契约：DB_SCHEMA.md #CircuitBreaker, PRD #F09
- 验收标准：CLOSED/OPEN/HALF-OPEN 状态机，连续失败阈值触发 OPEN

### [x] TASK-C10：OpenTelemetry 追踪注入
- 对应契约：PRD #F10, DB_SCHEMA.md #PipelineContext.trace_id
- 验收标准：每个请求生成 trace_id，注入 OTel span，propagated 到下游 LLM 调用

### [x] TASK-C11：Prometheus 指标采集
- 对应契约：API_CONTRACT.md #GET /metrics
- 验收标准：暴露 requests_total/request_duration/cache_hits/tokens/cost/circuit_breaker 等指标

### [x] TASK-C12：结构化 JSON 日志
- 对应契约：TECH_SPEC.md #日志格式
- 验收标准：structlog JSON 输出，包含 trace_id/request_id/user_id/level/event

## aigateway-api（API 服务）

### [x] TASK-B01：实现 POST /v1/chat/completions
- 对应契约：API_CONTRACT.md #chat-completions
- 验收标准：非流式和流式响应均正确，支持 SSE chunking，返回字段与契约一致

### [x] TASK-B02：实现 GET /v1/models
- 对应契约：API_CONTRACT.md #get-models
- 验收标准：返回配置的模型列表，格式与 OpenAI 兼容

### [x] TASK-B03：实现 POST /v1/embeddings
- 对应契约：API_CONTRACT.md #embeddings
- 验收标准：调用 sentence-transformers 或 OpenAI 嵌入模型，返回向量

### [x] TASK-B04：实现 GET /admin/api-keys（列表）
- 对应契约：API_CONTRACT.md #list-api-keys
- 验收标准：分页返回 API Key 列表及配额使用情况

### [x] TASK-B05：实现 POST /admin/api-keys（创建）
- 对应契约：API_CONTRACT.md #create-api-key
- 验收标准：创建新 Key，写入 Redis，通过 Pub/Sub 同步

### [x] TASK-B06：实现 DELETE /admin/api-keys/{key_id}
- 对应契约：API_CONTRACT.md #delete-api-key
- 验收标准：撤销 Key，Redis 删除，Pub/Sub 广播

### [x] TASK-B07：实现 GET /admin/quotas/{key_id}
- 对应契约：API_CONTRACT.md #get-quota
- 验收标准：返回详细配额使用、速率限制、告警信息

### [x] TASK-B08：实现 GET /metrics
- 对应契约：API_CONTRACT.md #get-metrics
- 验收标准：返回 Prometheus 格式指标文本

### [x] TASK-B09：实现 GET /health
- 对应契约：API_CONTRACT.md #get-health
- 验收标准：返回健康状态和依赖服务状态

### [x] TASK-B10：FastAPI 应用入口 + 路由挂载 + auth_middleware
- 对应契约：TECH_SPEC.md #项目结构
- 验收标准：uvicorn 启动，/v1/* 和 /admin/* 路由正确挂载，中间件校验 API Key

## aigateway-cli（CLI 工具）

### [x] TASK-CLI01：CLI 入口 + 交互式对话
- 对应契约：PRD #F07, US05
- 验收标准：`aigateway chat` 进入交互模式，`aigateway run --prompt "..."` 单次请求

### [x] TASK-CLI02：会话管理
- 对应契约：PRD #F07
- 验收标准：`--session name` 持久化对话历史到本地 JSON 文件
