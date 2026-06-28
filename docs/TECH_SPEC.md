# 技术规格说明
> 版本: 1.0 | 日期: 2026-06-27

## 技术栈选型

| 层级 | 技术选型 | 选型理由 |
|------|---------|---------|
| 后端框架 | FastAPI 0.110+ + Uvicorn 0.29+ (standard) | 原生 async/await 支持，Pydantic 自动校验，自动生成 OpenAPI 文档，适合高并发 I/O 密集型网关 |
| 多模型路由 | LiteLLM 1.40+ | 统一 OpenAI 兼容接口对接下游多提供商（OpenAI/Anthropic/Gemini/Bedrock/Ollama），内置 Router、CostTracker、Fallback 链 |
| 插件管线 | 自研 PipelineEngine（aigateway-core） | 需要精确控制插件执行顺序、依赖声明、短路机制、故障隔离 |
| 嵌入模型 | sentence-transformers 2.6+ (all-MiniLM-L6-v2) | 本地运行，零外部依赖，MVP 不需要外部嵌入 API |
| 向量缓存 | Qdrant 1.7+ | 高性能向量相似度搜索，原生 REST API + Python 客户端，支持 Payload 过滤 |
| KV 缓存 | Redis 7 (hiredis 驱动) | 低延迟 KV 读写，支持 Pub/Sub 分布式同步，连接池复用 |
| 指标采集 | Prometheus Client 0.20+ | Python 原生 SDK，轻量级，Grafana 生态兼容 |
| 链路追踪 | OpenTelemetry SDK 1.24+ | 行业标准，支持 trace_id 贯穿全管线，Prometheus 指标与 Trace 关联 |
| 结构化日志 | structlog 24+ | JSON 格式输出，支持 trace_id/request_id/user_id 自动注入 |
| 配置热加载 | PyYAML 6+ + Watchdog 3+ | YAML 解析 + 文件系统监听，原子交换实现无中断热重载 |
| 进程缓存 | cachetools 5.3+ (LRUCache) | 纯 Python 实现，线程安全，零网络开销 |
| 熔断器 | pybreaker 1.1+ | 轻量级 Circuit Breaker 实现，支持 per-provider 独立状态 |
| 前端框架 | React 18 + TypeScript + Vite | SPA 控制面板，TypeScript 强类型保障，Vite 快速热更新 |
| 图表库 | Recharts | React 原生图表库，与 TailwindCSS 风格一致 |
| 样式框架 | TailwindCSS | 原子化 CSS，快速构建控制面板 UI |
| 部署编排 | Docker Compose | 一键启动全部服务（Gateway + Dashboard + Qdrant + Redis + Prometheus + Grafana） |

## 项目目录结构

### 根目录
```
gateway/
├── docker-compose.yml              # 全部服务编排
├── .env.example                    # 环境变量模板
├── config.yaml                     # YAML 配置文件（热加载源）
│
├── aigateway-core/                 # 共享核心库
│   ├── pyproject.toml
│   ├── src/aigateway_core/
│   │   ├── __init__.py
│   │   ├── pipeline.py             # 异步插件管线引擎
│   │   ├── plugin_registry.py      # 插件注册、排序、依赖校验
│   │   ├── config.py               # YAML 配置加载器 + 环境变量覆盖 + 热重载
│   │   ├── litellm_bridge.py       # LiteLLM 封装（Router、Completion、CostTracker）
│   │   ├── context.py              # PipelineContext 共享状态
│   │   ├── security.py             # API Key 认证、配额管理器、PII 处理器
│   │   ├── caching.py              # 三级缓存（L1 LRUCache / L2 Redis / L3 Qdrant）
│   │   ├── circuit_breaker.py      # 熔断器（per-provider）
│   │   ├── tracing.py              # OpenTelemetry trace ID 注入与传播
│   │   ├── logger.py               # 结构化 JSON 日志（structlog）
│   │   └── metrics.py              # Prometheus 指标定义与采集
│   ├── tests/
│   └── requirements.txt
│
├── aigateway-api/                  # API Gateway（FastAPI 服务）
│   ├── src/aigateway_api/
│   │   ├── __init__.py
│   │   ├── main.py                 # FastAPI 应用入口，/v1/* 路由挂载
│   │   ├── openai_compat.py        # OpenAI API 兼容层（chat completions、models、embeddings）
│   │   ├── admin_routes.py         # 管理接口（API Key CRUD、配额查询）
│   │   ├── auth_middleware.py      # API Key 校验中间件
│   │   ├── streaming.py            # SSE 流式响应封装
│   │   └── routes.py               # /metrics、/health 等基础设施路由
│   ├── tests/
│   ├── Dockerfile
│   └── requirements.txt
│
├── aigateway-cli/                  # CLI 工具
│   ├── src/aigateway_cli/
│   │   ├── __init__.py
│   │   ├── __main__.py             # entrypoint: aigateway
│   │   ├── chat.py                 # 交互式对话（rich + prompt-toolkit）
│   │   ├── run.py                  # 单次请求
│   │   └── session.py              # 会话管理（本地 JSON 存储）
│   ├── tests/
│   └── requirements.txt
│
├── aigateway-ide/                  # IDE 扩展（TypeScript）
│   ├── src/
│   │   ├── extension.ts            # VS Code / Cursor 扩展入口
│   │   └── http-proxy.ts           # 请求拦截代理
│   ├── package.json
│   └── tsconfig.json
│
├── control-panel/                  # Web 控制面板（React SPA）
│   ├── package.json
│   ├── vite.config.ts
│   ├── src/
│   │   ├── api/                    # API 客户端（调用 Gateway Admin API）
│   │   ├── components/             # 可复用 UI 组件（图表、开关、表格）
│   │   ├── pages/
│   │   │   ├── Overview.tsx        # QPS/延迟/成本总览
│   │   │   ├── Plugins.tsx         # 插件开关与配置管理
│   │   │   ├── Costs.tsx           # 费用分析
│   │   │   ├── Quotas.tsx          # 配额管理
│   │   │   ├── Cache.tsx           # 缓存命中率
│   │   │   └── Logs.tsx            # 请求日志（含 trace_id）
│   │   ├── types.ts                # TypeScript 类型定义（与 API_CONTRACT 对齐）
│   │   └── App.tsx                 # 路由与布局
│   ├── public/
│   ├── Dockerfile
│   ├── nginx.conf
│   ├── .env                        # 本地开发
│   └── .env.production             # 生产构建
│
└── infra/
    ├── prometheus/prometheus.yml   # Prometheus 配置
    ├── grafana/
    │   ├── datasources.yml         # 数据源配置（指向 Prometheus）
    │   └── dashboards/            # 预置 Dashboard JSON 文件
    └── init/qdrant_collections/    # Qdrant 集合初始化脚本
```

## 全局规范

| 规范项 | 规则 |
|--------|------|
| Python 命名 | snake_case（变量、函数、模块），PascalCase（类名） |
| Python 类型注解 | 所有函数签名必须标注类型，返回值使用 `-> Type` |
| Python 错误处理 | 自定义异常层次：`GatewayError` -> 子类（`AuthError`, `QuotaExceededError`, `CircuitBreakerOpenError`） |
| TypeScript 命名 | camelCase（变量、属性），PascalCase（接口、组件），UPPER_SNAKE_CASE（常量） |
| 时间格式 | ISO 8601（`2024-01-15T08:30:00Z`），内部存储 UTC |
| 金额格式 | 浮点数，单位美元（如 `0.12` 表示 $0.12） |
| 日志格式 | JSON（structlog），必须包含 `trace_id`、`request_id`、`timestamp`、`level`、`event` 字段 |
| 统一错误格式 | `{ "error": { "code": "error_code", "message": "人类可读描述" } }` |
| 分页参数 | `page`（从 1 开始）、`pageSize`（默认 20，最大 100） |
| API 路径前缀 | 所有业务接口统一使用 `/v1/` 前缀（OpenAI 兼容） |
| 管理接口前缀 | 所有管理接口统一使用 `/admin/` 前缀 |

## 环境变量

| 变量名 | 说明 | 必填 | 默认值 | 示例值 |
|--------|------|------|--------|--------|
| `AI_GATEWAY_CONFIG_PATH` | YAML 配置文件路径 | 否 | `./config.yaml` | `/etc/aigateway/config.yaml` |
| `AI_GATEWAY_LOG_LEVEL` | 日志级别 | 否 | `info` | `debug` |
| `AI_GATEWAY_REDIS_URL` | Redis 连接地址 | 否 | `redis://localhost:6379/0` | `redis://redis:6379/0` |
| `AI_GATEWAY_QDRANT_URL` | Qdrant 连接地址 | 否 | `http://localhost:6333` | `http://qdrant:6333` |
| `AI_GATEWAY_HOST` | 监听主机地址 | 否 | `0.0.0.0` | `127.0.0.1` |
| `AI_GATEWAY_PORT` | 监听端口 | 否 | `8000` | `8000` |
| `AI_GATEWAY_WORKERS` | Uvicorn worker 数 | 否 | `4` | `4` |
| `AI_GATEWAY_EMBEDDING_BACKEND` | 嵌入模型后端 | 否 | `sentence_transformers` | `openai` |
| `AI_GATEWAY_OTEL_SAMPLE_RATE` | OTel 采样率 | 否 | `0.1` | `1.0` |
| `AI_GATEWAY_PROMETHEUS_ENABLED` | 是否暴露 Prometheus 指标 | 否 | `true` | `true` |
| `AI_GATEWAY_API_KEYS` | 预置 API Key（逗号分隔，仅单实例模式） | 否 | 空 | `sk-key1,sk-key2` |
| `OPENAI_API_KEY` | OpenAI 提供商密钥 | 条件必填 | — | `sk-proj-xxx` |
| `ANTHROPIC_API_KEY` | Anthropic 提供商密钥 | 条件必填 | — | `sk-ant-xxx` |
| `GEMINI_API_KEY` | Google Gemini 密钥 | 条件必填 | — | `AIza-xxx` |
| `VITE_API_BASE` | 前端 API 请求基础路径 | 否 | 空字符串（本地） | `/aigateway`（生产） |
| `VITE_BASE_URL` | Vite 构建基础路径 | 否 | `/`（本地） | `/aigateway/`（生产） |

## 部署路径规范（子路径部署）

> 本 Gateway 通过统一网关以 `/{APP_PATH}/` 形式对外服务。前端控制面板和 API 层的交互必须遵守以下约定。

| 配置项 | 本地开发值 | 生产值 | 说明 |
|--------|-----------|--------|------|
| `VITE_API_BASE` | `` （空字符串） | `/{APP_PATH}` | 前端 axios/fetch 基础路径 |
| `VITE_BASE_URL` | `/` | `/{APP_PATH}/` | Vite build base，影响静态资源路径 |
| vite.config.ts `base` | `/` | `/{APP_PATH}/` | 与 `VITE_BASE_URL` 保持一致 |

**前端 API 调用层必须用如下模式（禁止其他写法）：**
```ts
// ✅ 正确 — 使用环境变量前缀
const API_BASE = import.meta.env.VITE_API_BASE ?? ''
axios.get(`${API_BASE}/admin/api-keys`)

// ❌ 禁止 — 硬编码绝对路径，子路径部署时必定 404
axios.get('/admin/api-keys')
```

**frontend/.env 文件约定：**
```
# .env（本地开发）
VITE_API_BASE=
VITE_BASE_URL=/

# .env.production（生产构建）
VITE_API_BASE=/{APP_PATH}
VITE_BASE_URL=/{APP_PATH}/
```

> API_CONTRACT.md 中所有接口路径写相对路径（如 `/v1/chat/completions`），前缀由 `VITE_API_BASE` 在运行时拼接，**契约文件本身不含部署前缀**。

## 配置文件格式 — config.yaml 完整 Schema

```yaml
# ============================================================
# server: Gateway 服务配置
# ============================================================
server:
  host: string                      # 监听地址，默认 "0.0.0.0"
  port: integer                     # 监听端口，默认 8000，范围 1-65535
  workers: integer                  # Uvicorn worker 数，默认 4，范围 1-32

# ============================================================
# auth: API Key 认证与配额
# ============================================================
auth:
  api_keys:                         # 预置 API Key 列表（单实例模式使用）
    - key: string                   # API Key 值，格式 "sk-{prefix}-{random}"
      user_id: string               # 关联的用户 ID
      quotas:                       # 该 Key 的配额限制
        daily_tokens: integer       # 每日 token 上限，默认 1000000
        monthly_cost: float         # 每月成本上限（美元），默认 50.00
        rate_limit_rpm: integer     # 每分钟请求数上限，默认 60
        rate_limit_tpm: integer     # 每分钟 token 数上限，默认 100000
  distributed_mode: boolean         # 是否启用分布式模式（多实例 + Redis Pub/Sub 同步），默认 false

# ============================================================
# plugins: 插件管线配置
# ============================================================
plugins:
  - name: string                    # 插件名称，枚举:
                                    #   prompt_compress / prompt_cache / semantic_cache /
                                    #   conv_compressor / rag_retriever / model_router /
                                    #   mcp_optimizer / response_formatter / pii_detector
    enabled: boolean                # 是否启用，默认 true
    depends_on: array[string]       # 依赖的插件名称列表，默认 []
    config: object                  # 插件特定配置（见下方各插件详述）

# --- 各插件 config 字段详述 ---

# prompt_compress / pii_detector:
config.pii_strategy: string         # "sanitize" | "reject" | "hash"，默认 "sanitize"
config.pii_patterns: object         # PII 正则模式字典（详见设计文档 pii_patterns 章节）
config.max_prompt_size_bytes: integer  # PII 检测的最大提示词字节数，默认 52428 (50KB)

# prompt_cache:
config.ttl: integer                 # 缓存生存时间（秒），默认 3600
config.backend: string              # "redis" | "memory"，默认 "redis"
config.l1_maxsize: integer          # L1 缓存最大条目数，默认 1000
config.l2_maxsize: integer          # L2 Redis 缓存最大条目数，默认 100000

# semantic_cache:
config.threshold: float             # 语义相似度阈值，范围 0.0-1.0，默认 0.95
config.ttl: integer                 # 缓存生存时间（秒），默认 86400
config.backend: string              # "qdrant"，固定值
config.collection_name: string      # Qdrant 集合名，默认 "semantic_cache"

# model_router:
config.strategy: string             # "cost" | "speed" | "quality"，默认 "quality"
config.fallback: array[object]      # 降级模型列表 [{model: string, provider: string}]
config.retries: integer             # 最大重试次数，默认 3
config.retry_delay_ms: integer      # 重试间隔（毫秒），默认 1000

# circuit_breaker (per-provider):
config.providers.[name].failure_threshold: integer  # 连续失败次数阈值，默认 5
config.providers.[name].recovery_timeout: integer   # 恢复超时（秒），默认 60

# rag_retriever:
config.top_k: integer               # 检索文档数量，默认 20
config.rerank_top_k: integer        # Rerank 后保留数量，默认 3
config.document_loader: string      # "auto" | "pdf" | "txt" | "csv" | "json" | "markdown"
config.text_splitter.chunk_size: integer  # 文本分块大小，默认 512
config.text_splitter.chunk_overlap: integer # 重叠字符数，默认 64

# conv_compressor:
config.max_history: integer         # 保留的最大历史消息数，默认 10
config.summary_interval: integer    # 摘要压缩间隔（消息数），默认 5

# mcp_optimizer:
config.max_result_size_kb: integer  # MCP 结果最大尺寸（KB），默认 10
config.redis_ttl_seconds: integer   # Redis 大对象 TTL（秒），默认 3600

# response_formatter:
config.format: string               # "json" | "text"，默认 "json"
config.max_tokens: integer          # 响应最大 token 数，默认 200

# ============================================================
# providers: 下游 LLM 提供商配置
# ============================================================
providers:
  [provider_name]:                  # 枚举: openai / anthropic / gemini / bedrock / ollama
    api_key: string                 # 提供商 API Key（支持 ${ENV_VAR} 引用环境变量）
    base_url: string                # 自定义端点地址（Ollama 等）
    model_grouper:                  # LiteLLM 模型分组与降级
      - models: array[string]       # 组内模型列表
        fallback_models: array[string]  # 降级目标模型列表
    num_retries: integer            # 每组最大重试次数，默认 3
    retry_after: integer            # 重试间隔（毫秒），默认 1000

# ============================================================
# embedding: 嵌入模型配置（用于语义缓存）
# ============================================================
embedding:
  backend: string                   # "sentence_transformers" | "openai"
  model: string                     # sentence_transformers 模型名，默认 "all-MiniLM-L6-v2"
  openai_model: string              # OpenAI 嵌入模型名，默认 "text-embedding-3-small"

# ============================================================
# observability: 可观测性配置
# ============================================================
observability:
  prometheus_enabled: boolean       # 是否暴露 /metrics 端点，默认 true
  opentelemetry_enabled: boolean    # 是否启用 OTel 追踪，默认 true
  otel_service_name: string         # OTel 服务名，默认 "ai-gateway"
  otel_sample_rate: float           # 采样率，范围 0.0-1.0，默认 0.1
  log_format: string                # "json" | "text"，默认 "json"
  log_level: string                 # "debug" | "info" | "warning" | "error"，默认 "info"
  grafana_dashboard_json_path: string  # Grafana Dashboard JSON 文件路径（可选）
```
