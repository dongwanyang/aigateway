# AI Gateway

> 位于客户端和 LLM 提供商之间的 Token 优化网关 — 零代码修改，开箱即用的 OpenAI API 兼容代理。

[![Python](https://img.shields.io/badge/Python-3.12%2B-blue)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)

---

## 一句话介绍

把你的现有 AI 应用的 `OPENAI_BASE_URL` 指向 AI Gateway，即可自动享受 **提示词压缩、多级缓存、模型路由、PII 脱敏、熔断降级** 等能力，无需修改一行业务代码。

## 核心价值

| 价值 | 说明 |
|------|------|
| **零代码侵入** | 兼容 OpenAI API 格式，改一个 `base_url` 即可接入 |
| **成本降低** | 三级缓存（L1/L2/L3）命中时直接返回，LLM 调用成本趋近于零 |
| **响应加速** | L1 缓存 <1ms 返回，比直接调 LLM 快数十倍 |
| **安全合规** | 自动检测并脱敏 20+ 类 PII 敏感信息（身份证、邮箱、银行卡等） |
| **高可用** | 熔断器 + 模型降级链，单一提供商故障不影响整体服务 |
| **全链路可观测** | Prometheus 指标 + OpenTelemetry 追踪 + 结构化 JSON 日志 |

## 四种使用形式

| 形式 | 说明 | 典型场景 |
|------|------|---------|
| **API Gateway** | FastAPI 服务，OpenAI 兼容接口 | 现有应用替换 `base_url` |
| **CLI 工具** | `pip install aigateway-cli`，终端交互式对话 | 快速测试 Prompt 和模型效果 |
| **控制面板** | React SPA，可视化监控与管理 | 运维人员查看用量、管理配额 |
| **IDE 扩展** | VS Code / Cursor 插件（规划中） | 自动拦截 AI 请求，零配置优化 |

---

## 功能一览

### MVP 已实现

| 功能 | 状态 | 说明 |
|------|------|------|
| F01: API Gateway 代理 | ✅ | `/v1/chat/completions`、`/v1/models`、`/v1/embeddings`，流式/非流式 |
| F02: 插件管线引擎 | ✅ | 异步插件管线 + 5 个内置插件，支持依赖声明和短路 |
| F03: 三级缓存系统 | ✅ | L1 进程内存 → L2 Redis (LZ4) → L3 Qdrant 向量 |
| F04: 模型路由与降级 | ✅ | LiteLLM Router，cost/speed/quality 策略，fallback 链 |
| F05: API Key 认证与配额 | ✅ | per-key 日 token/月成本/RPM/TPM 限制，Redis Pub/Sub 同步 |
| F06: PII 检测与脱敏 | ✅ | sanitize / reject / hash 三种策略，20+ 类敏感信息 |
| F07: CLI 交互工具 | ✅ | `aigateway chat` 交互式对话，`aigateway run` 单次请求 |
| F08: 配置热加载 | ✅ | YAML 配置 + watchdog 文件监听，原子交换 |
| F09: 熔断器 | ✅ | per-provider CLOSED/OPEN/HALF-OPEN 状态机 |
| F10: 可观测性 | ✅ | Prometheus 指标 + OpenTelemetry 追踪 + structlog JSON 日志 |
| F13: Docker Compose | ⏳ | 部署编排文件规划中 |
| F14: 提示词压缩 | ✅ | 管线骨架预留，压缩算法待接入 |
| F15: 流式缓存响应 | ✅ | 缓存命中时 SSE 分块模拟（20ms/chunk） |
| F16: 插件故障隔离 | ✅ | 非关键插件失败 fail-open，请求继续 |

### 本期排除（v2+）

侧边栏部署、DAG 并行调度、异步任务队列、Feature Flag、RAG 文档检索、MCP 工具调用优化、对话摘要压缩、响应格式器。

---

## 架构图

```
客户端 (OpenAI SDK / CLI / IDE)
        │
        ▼
┌─────────────────────┐
│   AI Gateway API    │  FastAPI + Uvicorn
│   :8000             │
│                     │
│  ┌─────────────────┐│
│  │  插件管线 Pipeline││
│  │  ┌───┐ ┌───┐    ││
│  │  │PII│→│Cache│──┼┼── 命中 → 直接返回 (<1ms)
│  │  └───┘ └───┘    ││
│  │       ↓ 未命中   ││
│  │  ┌──────────┐   ││
│  │  │模型路由    │   ││
│  │  │fallback   │   ││
│  │  └──────────┘   ││
│  └─────────────────┘│
└─────────┬───────────┘
          │
    ┌─────┼──────┬──────────┐
    ▼     ▼      ▼          ▼
  OpenAI  Anthropic  Gemini  Bedrock  Ollama
  (下游 LLM 提供商)
```

---

## 快速开始

### 前置要求

- Python 3.12+
- Redis 7+
- Qdrant 1.7+（可选，用于 L3 语义缓存）

### 安装

```bash
# 安装核心库
cd aigateway-core
pip install -e .

# 安装 API 服务
cd ../aigateway-api
pip install -e .

# 安装 CLI 工具
cd ../aigateway-cli
pip install -e .
```

### 配置

创建 `config.yaml`：

```yaml
server:
  host: "0.0.0.0"
  port: 8000
  workers: 4

providers:
  openai:
    api_key: "${OPENAI_API_KEY}"
    model_grouper:
      - models: ["gpt-4o", "gpt-4o-mini"]
        fallback_models: ["gpt-3.5-turbo"]
  anthropic:
    api_key: "${ANTHROPIC_API_KEY}"

plugins:
  - name: pii_detector
    enabled: true
    config:
      pii_strategy: "sanitize"
  - name: prompt_cache
    enabled: true
    config:
      ttl: 3600
      l1_maxsize: 1000
  - name: model_router
    enabled: true
    config:
      strategy: "cost"
```

### 启动

#### 方式一：本地开发

```bash
# 1. 准备环境变量
cp .env.example .env
# 编辑 .env，填入 OPENAI_API_KEY 等

# 2. 安装依赖
pip install -r aigateway-api/requirements.txt  # 如需要

# 3. 启动 API 服务
cd aigateway-api
uvicorn src.aigateway_api.main:create_app --factory --host 0.0.0.0 --port 8000 --reload

# 4. 启动前端（另一个终端）
cd ../control-panel
npm install
npm run dev
# 访问 http://localhost:5173
```

#### 方式二：Docker Compose（一键部署）

```bash
# 1. 准备环境变量
cp .env.example .env
# 编辑 .env，填入 OPENAI_API_KEY 等

# 2. 一键启动全部服务
docker compose up -d

# 3. 访问
# API Gateway:   http://localhost:8000
# 控制面板:      http://localhost:3000
# Prometheus:    http://localhost:9090
# Grafana:       http://localhost:3001  (admin/admin)
```

### 使用

```bash
# 测试：列出模型
curl http://localhost:8000/v1/models \
  -H "Authorization: Bearer sk-test-key"

# 测试：发送聊天请求
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-test-key" \
  -d '{
    "model": "gpt-4o",
    "messages": [{"role": "user", "content": "你好，世界"}]
  }'

# CLI 交互式对话
aigateway chat

# CLI 单次请求
aigateway run --prompt "你好，世界"
```

---

## 项目结构

```
gateway2/
├── aigateway-core/          # 共享核心库
│   └── src/aigateway_core/
│       ├── pipeline.py      # 异步插件管线引擎
│       ├── plugin_registry.py # 插件注册与依赖校验
│       ├── config.py        # YAML 配置加载器 + 热重载
│       ├── litellm_bridge.py # LiteLLM 封装（Router/CostTracker）
│       ├── context.py       # PipelineContext 共享状态
│       ├── security.py      # API Key 认证、配额、PII 检测
│       ├── caching.py       # 三级缓存（L1/L2/L3）
│       ├── circuit_breaker.py # 熔断器
│       ├── tracing.py       # OpenTelemetry 追踪
│       ├── metrics.py       # Prometheus 指标
│       ├── logger.py        # 结构化 JSON 日志
│       ├── redis_client.py  # Redis 连接管理
│       ├── qdrant_client.py # Qdrant 连接管理
│       └── exceptions.py    # 异常层次定义
│
├── aigateway-api/           # API 服务
│   └── src/aigateway_api/
│       ├── main.py          # FastAPI 应用入口
│       ├── openai_compat.py # OpenAI 兼容接口
│       ├── admin_routes.py  # 管理接口（API Key CRUD）
│       ├── auth_middleware.py # API Key 校验中间件
│       ├── streaming.py     # SSE 流式响应
│       └── routes.py        # /metrics, /health
│
├── aigateway-cli/           # CLI 工具
│   └── src/aigateway_cli/
│       ├── __main__.py      # 入口: aigateway
│       ├── chat.py          # 交互式对话
│       ├── run.py           # 单次请求
│       └── session.py       # 会话管理
│
├── docs/                    # 设计文档
│   ├── PRD.md               # 产品需求文档
│   ├── TECH_SPEC.md         # 技术规格说明
│   ├── API_CONTRACT.md      # API 契约
│   ├── DB_SCHEMA.md         # 数据库 Schema
│   ├── DESIGN_SYSTEM.md     # 前端设计规范
│   └── BACKEND_STATUS.md    # 后端实现状态追踪
│
└── project-tasks/           # 任务清单
    ├── backend-tasklist.md
    └── frontend-tasklist.md
```

---

## API 接口

### OpenAI 兼容接口 (`/v1/`)

| 方法 | 路径 | 说明 | 鉴权 |
|------|------|------|------|
| POST | `/v1/chat/completions` | 聊天补全（流式/非流式） | ✅ |
| GET | `/v1/models` | 列出可用模型 | ✅ |
| POST | `/v1/embeddings` | 生成嵌入向量 | ✅ |

### 管理接口 (`/admin/`)

| 方法 | 路径 | 说明 | 鉴权 |
|------|------|------|------|
| GET | `/admin/api-keys` | 列出 API Key（分页） | ✅ (admin) |
| POST | `/admin/api-keys` | 创建 API Key | ✅ (admin) |
| DELETE | `/admin/api-keys/{key_id}` | 撤销 API Key | ✅ (admin) |
| GET | `/admin/quotas/{key_id}` | 查询配额详情 | ✅ (admin) |

### 基础设施接口

| 方法 | 路径 | 说明 | 鉴权 |
|------|------|------|------|
| GET | `/metrics` | Prometheus 指标 | ❌ |
| GET | `/health` | 健康检查 | ❌ |

### 错误格式

所有错误统一返回：

```json
{
  "error": {
    "code": "error_code",
    "message": "人类可读描述"
  }
}
```

---

## 环境变量

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `AI_GATEWAY_CONFIG_PATH` | YAML 配置文件路径 | `./config.yaml` |
| `AI_GATEWAY_LOG_LEVEL` | 日志级别 | `info` |
| `AI_GATEWAY_REDIS_URL` | Redis 连接地址 | `redis://localhost:6379/0` |
| `AI_GATEWAY_QDRANT_URL` | Qdrant 连接地址 | `http://localhost:6333` |
| `AI_GATEWAY_HOST` | 监听地址 | `0.0.0.0` |
| `AI_GATEWAY_PORT` | 监听端口 | `8000` |
| `AI_GATEWAY_WORKERS` | Uvicorn worker 数 | `4` |
| `AI_GATEWAY_OTEL_SAMPLE_RATE` | OTel 采样率 | `0.1` |
| `OPENAI_API_KEY` | OpenAI 提供商密钥 | — |
| `ANTHROPIC_API_KEY` | Anthropic 提供商密钥 | — |
| `GEMINI_API_KEY` | Google Gemini 密钥 | — |

---

## 技术栈

| 层级 | 技术 | 用途 |
|------|------|------|
| 后端框架 | FastAPI + Uvicorn | 高并发异步 API |
| 多模型路由 | LiteLLM | 统一 OpenAI 兼容接口对接多提供商 |
| 三级缓存 | cachetools + Redis + Qdrant | L1 进程 <1ms / L2 Redis <5ms / L3 向量 <50ms |
| 嵌入模型 | sentence-transformers | L3 语义缓存向量化 |
| 熔断器 | 自研状态机 | per-provider 独立熔断 |
| 指标采集 | Prometheus | 请求数/延迟/缓存命中率/token 消耗 |
| 链路追踪 | OpenTelemetry | 全链路 trace_id 贯穿 |
| 结构化日志 | structlog | JSON 格式，自动注入 trace_id |
| 前端（规划） | React + TypeScript + Vite + TailwindCSS | 控制面板 |

---

## 开发

### 运行测试

```bash
# PII 检测测试
python -m pytest aigateway-core/tests/test_security.py -v

# 熔断器测试
python -m pytest aigateway-core/tests/test_circuit_breaker.py -v
```

### 代码质量

- Python: 全部函数签名含类型注解
- 异常层次: `GatewayError` → `AuthError` / `QuotaExceededError` / `CircuitBreakerOpenError`
- 所有模块通过 AST 编译检查和导入检查

---

## 文档

| 文档 | 说明 |
|------|------|
| [PRD.md](docs/PRD.md) | 产品需求文档，用户故事与验收标准 |
| [TECH_SPEC.md](docs/TECH_SPEC.md) | 技术规格，选型与配置 Schema |
| [API_CONTRACT.md](docs/API_CONTRACT.md) | API 契约，请求/响应格式 |
| [DB_SCHEMA.md](docs/DB_SCHEMA.md) | 数据库 Schema，Redis Key / Qdrant 集合 |
| [DESIGN_SYSTEM.md](docs/DESIGN_SYSTEM.md) | 前端设计规范，颜色/字体/组件 |
| [BACKEND_STATUS.md](docs/BACKEND_STATUS.md) | 后端实现状态追踪 |

---

## 许可证

MIT
