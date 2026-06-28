# API 接口契约
> 版本: 1.0 | 基础路径: /api/v1
> 业务路径前缀: /v1/ (OpenAI 兼容) 和 /admin/ (管理接口)
> 注意: 部署前缀由 VITE_API_BASE 在运行时拼接，本文件不包含子路径前缀

## 认证说明

### 业务接口（/v1/*）
所有 `/v1/*` 接口必须在请求头中携带有效 API Key，二选一：
```
Authorization: Bearer {API_KEY}
```
或
```
x-api-key: {API_KEY}
```

### 管理接口（/admin/*）
所有 `/admin/*` 接口必须在请求头中携带管理员权限的 API Key：
```
Authorization: Bearer {ADMIN_API_KEY}
```

### Token 过期处理
Token 无效或过期时返回 401，前端需引导用户重新认证。

## 统一响应格式

**成功**：
```json
{ "data": { ... }, "message": "success" }
```

**失败**：
```json
{ "error": { "code": "error_code", "message": "人类可读的错误描述" } }
```

**分页成功**：
```json
{
  "data": {
    "items": [ ... ],
    "pagination": {
      "page": 1,
      "pageSize": 20,
      "total": 100
    }
  },
  "message": "success"
}
```

---

## 业务接口（OpenAI 兼容）

### POST /v1/chat/completions

**用途**：F01 — OpenAI 兼容聊天补全接口，支持非流式和流式响应。客户端将 `base_url` 指向 Gateway 后自动接入缓存、压缩、路由等管线能力。

**鉴权**：需要

**请求体** (`Content-Type: application/json`)：
```json
{
  "model": "gpt-4o",                          // 必填，字符串，目标模型名称
  "messages": [                                 // 必填，消息数组
    {
      "role": "system",                        // 必填，角色枚举: "system" | "user" | "assistant" | "tool"
      "content": "You are a helpful assistant." // 必填，消息内容字符串
    }
  ],
  "temperature": 0.7,                          // 选填，浮点数，范围 0.0-2.0，默认 1.0
  "max_tokens": 2048,                          // 选填，整数，默认下游模型上限
  "top_p": 1.0,                                // 选填，浮点数，范围 0.0-1.0，默认 1.0
  "frequency_penalty": 0.0,                    // 选填，浮点数，范围 -2.0-2.0，默认 0.0
  "presence_penalty": 0.0,                     // 选填，浮点数，范围 -2.0-2.0，默认 0.0
  "stream": false,                             // 选填，布尔值，是否启用 SSE 流式响应，默认 false
  "tools": [                                    // 选填，工具调用数组
    {
      "type": "function",                      // 必填，固定值 "function"
      "function": {                            // 必填，函数定义
        "name": "get_weather",                 // 必填，函数名
        "description": "Get the weather",      // 必填，函数描述
        "parameters": {                        // 必填，JSON Schema 对象
          "type": "object",
          "properties": {},
          "required": []
        }
      }
    }
  ],
  "tool_choice": "auto",                       // 选填，字符串: "auto" | "none" | "required" | { "type": "function", "function": { "name": "..." } }
  "stop": null,                                // 选填，字符串或字符串数组，最多 4 个停止序列
  "user": "usr-12345"                          // 选填，字符串，客户端提供的用户标识，用于审计
}
```

**成功响应 - 非流式 (200)**：
```json
{
  "data": {
    "id": "chatcmpl-abc123",                    // 字符串，聊天补全请求 ID
    "object": "chat.completion",                 // 字符串，固定值
    "created": 1705312200,                       // 整数，Unix 时间戳（秒）
    "model": "gpt-4o",                           // 字符串，实际使用的模型名称
    "choices": [
      {
        "index": 0,                              // 整数，选择项索引
        "finish_reason": "stop",                 // 字符串: "stop" | "length" | "tool_calls" | "content_filter"
        "message": {
          "role": "assistant",                   // 字符串，固定值 "assistant"
          "content": "Hello! How can I help you?", // 字符串，助手回复内容，null 表示工具调用
          "tool_calls": [                        // 选填，工具调用数组，仅在 finish_reason 为 "tool_calls" 时存在
            {
              "id": "call_xyz",                  // 字符串，工具调用 ID
              "type": "function",                // 字符串，固定值 "function"
              "function": {
                "name": "get_weather",           // 字符串，函数名
                "arguments": "{\"location\":\"Beijing\"}" // 字符串，JSON 编码的参数
              }
            }
          ]
        },
        "delta": null                            // 非流式响应中为 null
      }
    ],
    "usage": {                                   // 选填，令牌使用情况
      "prompt_tokens": 25,                       // 整数，输入 token 数
      "completion_tokens": 120,                  // 整数，输出 token 数
      "total_tokens": 145                        // 整数，总 token 数
    },
    "_meta": {                                   // 选填，管线元数据
      "cache_hit": false,                        // 布尔值，是否命中缓存
      "cache_tier": null,                        // 字符串或 null: "L1" | "L2" | "L3" | null
      "plugin_trace": [                          // 数组，各插件执行耗时
        {
          "plugin_name": "prompt_compress",      // 字符串，插件名称
          "duration_ms": 2.3,                    // 浮点数，耗时（毫秒）
          "status": "success"                    // 字符串: "success" | "skipped" | "failed"
        }
      ],
      "routed_to": {                             // 选填，路由信息
        "provider": "openai",                    // 字符串，实际调用的提供商
        "model": "gpt-4o",                       // 字符串，实际调用的模型
        "fallback_chain": []                     // 数组，经历了几次降级
      }
    }
  },
  "message": "success"
}
```

**成功响应 - 流式 (200)**：
流式响应以 SSE (Server-Sent Events) 格式返回，每个 chunk 格式如下：
```
data: {"id":"chatcmpl-abc123","object":"chat.completion.chunk","created":1705312200,"model":"gpt-4o","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}

data: {"id":"chatcmpl-abc123","object":"chat.completion.chunk","created":1705312200,"model":"gpt-4o","choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}

data: {"id":"chatcmpl-abc123","object":"chat.completion.chunk","created":1705312200,"model":"gpt-4o","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

每个 chunk 的完整结构：
```json
{
  "id": "chatcmpl-abc123",                    // 字符串，与原始请求 ID 一致
  "object": "chat.completion.chunk",           // 字符串，固定值
  "created": 1705312200,                       // 整数，Unix 时间戳（秒）
  "model": "gpt-4o",                           // 字符串，实际使用的模型
  "choices": [
    {
      "index": 0,                              // 整数，选择项索引
      "delta": {                               // 对象，增量内容
        "role": "assistant" | null,             // 字符串或 null，角色（首 chunk 有值）
        "content": "string" | null,             // 字符串或 null，增量文本内容
        "tool_calls": [                         // 选填，工具调用增量
          {
            "index": 0,                        // 整数，tool_call 索引
            "id": "call_xyz" | null,            // 字符串或 null
            "type": "function" | null,           // 字符串或 null
            "function": {
              "name": "string" | null,           // 字符串或 null
              "arguments": "string" | null        // 字符串或 null，增量 JSON 参数
            }
          }
        ]
      },
      "finish_reason": "stop" | "length" | "tool_calls" | null  // 字符串或 null
    }
  ],
  "usage": {                                   // 仅最后一个 chunk 包含
    "prompt_tokens": 25,                       // 整数
    "completion_tokens": 120,                  // 整数
    "total_tokens": 145                        // 整数
  }
}
```

**缓存命中流式响应特殊行为**（F15）：
- 缓存命中时，Gateway 将缓存的完整响应按 chunk 分块，以 20ms/chunk 的延迟模拟真实 LLM 生成
- 首个 chunk 的 `delta.role` 为 `"assistant"`，最后一个 chunk 的 `finish_reason` 为 `"stop"`
- 客户端无法区分缓存命中与真实 LLM 响应

**错误响应**：

| HTTP 状态码 | error.code | error.message | 触发条件 |
|------------|-----------|--------------|---------|
| 400 | `validation_error` | "Request body validation failed: ..." | 参数格式错误或缺少必填项 |
| 400 | `pii_rejected` | "PII detected in request: [category list]" | PII 策略为 reject 且检测到敏感信息 |
| 400 | `invalid_model` | "Model '{model}' is not configured" | 请求的模型未在 providers 中配置 |
| 401 | `unauthorized` | "Invalid or missing API key" | 未提供 API Key 或 Key 无效 |
| 403 | `forbidden` | "API key '{key_id}' has been revoked" | API Key 已被撤销 |
| 429 | `quota_exceeded_daily_tokens` | "Daily token limit exceeded: {used}/{limit}" | 日 token 配额已用完 |
| 429 | `quota_exceeded_monthly_cost` | "Monthly cost limit exceeded: ${used}/${limit}" | 月成本配额已用完 |
| 429 | `rate_limit_rpm` | "Rate limit exceeded: {current}/{limit} requests per minute" | RPM 速率限制触发 |
| 429 | `rate_limit_tpm` | "Rate limit exceeded: {current}/{limit} tokens per minute" | TPM 速率限制触发 |
| 429 | `retry_after_header_present` | "Rate limit exceeded" | 同上四类 429 之一，响应头携带 `Retry-After: {seconds}` |
| 503 | `circuit_breaker_open` | "Circuit breaker OPEN for provider '{provider}'" | 下游提供商熔断器处于 OPEN 状态 |
| 504 | `upstream_timeout` | "Upstream provider timed out after {timeout}ms" | 下游 LLM 提供商响应超时 |
| 500 | `internal_error` | "Internal gateway error: {details}" | 网关内部错误 |

---

### GET /v1/models

**用途**：F01 — 列出当前 Gateway 配置的可用模型列表，返回标准 OpenAI 格式。

**鉴权**：需要

**请求参数**：无

**成功响应 (200)**：
```json
{
  "data": {
    "object": "list",                            // 字符串，固定值 "list"
    "data": [
      {
        "id": "gpt-4o",                          // 字符串，模型 ID
        "object": "model",                       // 字符串，固定值 "model"
        "created": 1705312200,                   // 整数，Unix 时间戳（秒）
        "owned_by": "openai",                    // 字符串，提供商标识
        "permission": [                           // 数组，模型权限列表
          {
            "id": "perm_abc123",                 // 字符串，权限 ID
            "object": "model_permission",         // 字符串，固定值
            "created": 1705312200,               // 整数，Unix 时间戳
            "allow_create_engine": false,         // 布尔值，是否允许创建引擎
            "allow_sampling": true,               // 布尔值，是否允许采样
            "allow_logprobs": true,               // 布尔值，是否允许日志概率
            "allow_search_indices": false,         // 布尔值，是否允许搜索索引
            "allow_view": true,                   // 布尔值，是否允许查看
            "allow_fine_tuning": false,            // 布尔值，是否允许微调
            "organization": "*",                  // 字符串，组织限制，"*" 表示全局
            "group": null,                        // 字符串或 null，分组
            "is_blocking": false                  // 布尔值，是否被阻止
          }
        ]
      }
    ]
  },
  "message": "success"
}
```

**错误响应**：

| HTTP 状态码 | error.code | error.message | 触发条件 |
|------------|-----------|--------------|---------|
| 401 | `unauthorized` | "Invalid or missing API key" | 未提供 API Key 或 Key 无效 |
| 500 | `internal_error` | "Failed to fetch model list from providers" | 无法从下游提供商获取模型列表 |

---

### POST /v1/embeddings

**用途**：F03 — 生成文本的嵌入向量，用于语义缓存（L3）。Gateway 内部使用，也可供外部调用。

**鉴权**：需要

**请求体** (`Content-Type: application/json`)：
```json
{
  "model": "all-MiniLM-L6-v2",                 // 必填，字符串，嵌入模型名称
  "input": "Hello world",                       // 必填，字符串或字符串数组
  "user": "usr-12345"                           // 选填，字符串，客户端提供的用户标识
}
```

**成功响应 (200)**：
```json
{
  "data": {
    "object": "list",                            // 字符串，固定值 "list"
    "data": [
      {
        "object": "embedding",                   // 字符串，固定值 "embedding"
        "index": 0,                              // 整数，嵌入向量在输入中的索引
        "embedding": [0.01, -0.02, 0.03, ...]    // 数组，浮点数数组，向量维度取决于模型（all-MiniLM-L6-v2 为 384）
      }
    ],
    "usage": {
      "prompt_tokens": 2,                        // 整数，处理的 token 数
      "total_tokens": 2                          // 整数，总 token 数
    }
  },
  "message": "success"
}
```

**错误响应**：

| HTTP 状态码 | error.code | error.message | 触发条件 |
|------------|-----------|--------------|---------|
| 400 | `validation_error` | "Input must be a string or array of strings" | 输入格式错误 |
| 400 | `invalid_model` | "Embedding model '{model}' not found" | 指定的嵌入模型未配置 |
| 401 | `unauthorized` | "Invalid or missing API key" | 未提供 API Key 或 Key 无效 |
| 500 | `internal_error` | "Embedding generation failed: {details}" | 嵌入生成失败 |

---

## 管理接口

### GET /admin/api-keys

**用途**：F05 — 列出所有 API Key 及其配额使用情况。

**鉴权**：需要管理员权限

**请求参数**（Query String）：
| 参数名 | 类型 | 必填 | 默认值 | 说明 |
|--------|------|------|--------|------|
| `page` | integer | 否 | 1 | 页码，从 1 开始 |
| `pageSize` | integer | 否 | 20 | 每页数量，最大 100 |

**成功响应 (200)**：
```json
{
  "data": {
    "items": [
      {
        "id": "key_abc123",                      // 字符串，API Key ID（内部标识）
        "key_prefix": "sk-dev-xxx",              // 字符串，Key 的前 8 个字符，用于识别
        "user_id": "dev-user",                   // 字符串，关联的用户 ID
        "created_at": "2024-01-15T08:30:00Z",    // 字符串，ISO 8601 创建时间
        "last_used_at": "2024-01-20T14:00:00Z",  // 字符串或 null，ISO 8601 最后使用时间
        "status": "active",                      // 字符串: "active" | "revoked" | "suspended"
        "quotas": {
          "daily_tokens_used": 50000,            // 整数，今日已用 token 数
          "daily_tokens_limit": 1000000,         // 整数，每日 token 上限
          "monthly_cost_used": 2.50,             // 浮点数，本月已用成本（美元）
          "monthly_cost_limit": 50.00,           // 浮点数，每月成本上限（美元）
          "rpm_current": 5,                      // 整数，当前分钟请求数
          "rpm_limit": 60,                       // 整数，每分钟请求数上限
          "tpm_current": 1000,                   // 整数，当前分钟 token 数
          "tpm_limit": 100000                    // 整数，每分钟 token 数上限
        },
        "usage_percentage": {                    // 对象，使用比例
          "daily_tokens": 0.05,                  // 浮点数，日 token 使用比例 0.0-1.0
          "monthly_cost": 0.05                   // 浮点数，月成本使用比例 0.0-1.0
        }
      }
    ],
    "pagination": {
      "page": 1,                                 // 整数，当前页码
      "pageSize": 20,                            // 整数，每页数量
      "total": 1                                 // 整数，总记录数
    }
  },
  "message": "success"
}
```

**错误响应**：

| HTTP 状态码 | error.code | error.message | 触发条件 |
|------------|-----------|--------------|---------|
| 401 | `unauthorized` | "Invalid or missing API key" | 未提供 API Key 或 Key 无效 |
| 403 | `forbidden` | "Insufficient permissions" | 当前 Key 无管理权限 |
| 500 | `internal_error` | "Failed to list API keys" | 服务器内部错误 |

---

### POST /admin/api-keys

**用途**：F05 — 创建新的 API Key。

**鉴权**：需要管理员权限

**请求体** (`Content-Type: application/json`)：
```json
{
  "user_id": "new-user",                       // 必填，字符串，关联的用户 ID
  "daily_tokens": 1000000,                     // 选填，整数，每日 token 上限，默认 1000000
  "monthly_cost": 50.00,                       // 选填，浮点数，每月成本上限（美元），默认 50.00
  "rate_limit_rpm": 60,                        // 选填，整数，每分钟请求数上限，默认 60
  "rate_limit_tpm": 100000                     // 选填，整数，每分钟 token 数上限，默认 100000
}
```

**成功响应 (200)**：
```json
{
  "data": {
    "id": "key_def456",                        // 字符串，新创建的 API Key ID
    "key": "sk-dev-a1b2c3d4e5f6",              // 字符串，完整的 API Key 值（仅创建时返回一次）
    "key_prefix": "sk-dev-a1b",                 // 字符串，Key 的前 8 个字符
    "user_id": "new-user",                     // 字符串，关联的用户 ID
    "created_at": "2024-01-21T10:00:00Z",      // 字符串，ISO 8601 创建时间
    "status": "active",                        // 字符串，初始状态
    "quotas": {
      "daily_tokens": 1000000,                 // 整数，每日 token 上限
      "monthly_cost": 50.00,                   // 浮点数，每月成本上限（美元）
      "rate_limit_rpm": 60,                    // 整数，每分钟请求数上限
      "rate_limit_tpm": 100000                 // 整数，每分钟 token 数上限
    }
  },
  "message": "success"
}
```

**错误响应**：

| HTTP 状态码 | error.code | error.message | 触发条件 |
|------------|-----------|--------------|---------|
| 400 | `validation_error` | "user_id is required" | 缺少必填字段 user_id |
| 400 | `validation_error` | "daily_tokens must be a positive integer" | 配额参数格式错误 |
| 400 | `validation_error` | "monthly_cost must be a positive number" | 配额参数格式错误 |
| 409 | `conflict` | "User '{user_id}' already has an active key" | 同一 user_id 已存在活跃 Key |
| 401 | `unauthorized` | "Invalid or missing API key" | 未提供 API Key 或 Key 无效 |
| 403 | `forbidden` | "Insufficient permissions" | 当前 Key 无管理权限 |
| 500 | `internal_error` | "Failed to create API key" | 服务器内部错误 |

---

### DELETE /admin/api-keys/{key_id}

**用途**：F05 — 撤销（删除）指定的 API Key。撤销后该 Key 立即失效，所有 Gateway 实例通过 Redis Pub/Sub 同步。

**鉴权**：需要管理员权限

**路径参数**：
| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `key_id` | string | 是 | API Key 的内部 ID（如 "key_abc123"） |

**成功响应 (200)**：
```json
{
  "data": {
    "id": "key_abc123",                      // 字符串，被撤销的 Key ID
    "status": "revoked",                     // 字符串，新状态 "revoked"
    "revoked_at": "2024-01-21T10:00:00Z"     // 字符串，ISO 8601 撤销时间
  },
  "message": "success"
}
```

**错误响应**：

| HTTP 状态码 | error.code | error.message | 触发条件 |
|------------|-----------|--------------|---------|
| 400 | `validation_error` | "Invalid key_id format" | key_id 格式不正确 |
| 401 | `unauthorized` | "Invalid or missing API key" | 未提供 API Key 或 Key 无效 |
| 403 | `forbidden` | "Insufficient permissions" | 当前 Key 无管理权限 |
| 404 | `not_found` | "API key '{key_id}' not found" | 指定的 Key ID 不存在 |
| 500 | `internal_error` | "Failed to revoke API key" | 服务器内部错误 |

---

### GET /admin/quotas/{key_id}

**用途**：F05 — 查询指定 API Key 的详细配额使用和实时速率限制状态。

**鉴权**：需要管理员权限

**路径参数**：
| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `key_id` | string | 是 | API Key 的内部 ID |

**成功响应 (200)**：
```json
{
  "data": {
    "id": "key_abc123",                      // 字符串，API Key ID
    "user_id": "dev-user",                   // 字符串，关联的用户 ID
    "status": "active",                      // 字符串: "active" | "revoked" | "suspended"
    "quotas": {
      "daily_tokens": {
        "used": 500000,                      // 整数，今日已用 token 数
        "limit": 1000000,                    // 整数，每日 token 上限
        "reset_at": "2024-01-22T00:00:00Z"   // 字符串，ISO 8601，今日重置时间
      },
      "monthly_cost": {
        "used": 25.00,                       // 浮点数，本月已用成本（美元）
        "limit": 50.00,                      // 浮点数，每月成本上限（美元）
        "reset_at": "2024-02-01T00:00:00Z"   // 字符串，ISO 8601，本月重置时间
      },
      "rate_limit": {
        "rpm": {
          "current": 12,                     // 整数，当前分钟请求数
          "limit": 60                        // 整数，每分钟请求数上限
        },
        "tpm": {
          "current": 5000,                   // 整数，当前分钟 token 数
          "limit": 100000                    // 整数，每分钟 token 数上限
        }
      }
    },
    "alerts": [                              // 数组，活跃告警列表
      {
        "type": "budget_warning",            // 字符串: "budget_warning" | "rate_limit_warning"
        "threshold_percent": 80,             // 整数，触发告警的百分比
        "message": "Usage has reached 80% of monthly budget"  // 字符串，告警描述
      }
    ],
    "last_request_at": "2024-01-21T09:55:00Z", // 字符串或 null，ISO 8601 最后请求时间
    "total_requests_today": 150,             // 整数，今日总请求数
    "total_tokens_today": 500000             // 整数，今日总 token 数
  },
  "message": "success"
}
```

**错误响应**：

| HTTP 状态码 | error.code | error.message | 触发条件 |
|------------|-----------|--------------|---------|
| 400 | `validation_error` | "Invalid key_id format" | key_id 格式不正确 |
| 401 | `unauthorized` | "Invalid or missing API key" | 未提供 API Key 或 Key 无效 |
| 403 | `forbidden` | "Insufficient permissions" | 当前 Key 无管理权限 |
| 404 | `not_found` | "API key '{key_id}' not found" | 指定的 Key ID 不存在 |
| 500 | `internal_error` | "Failed to query quota" | 服务器内部错误 |

---

### GET /metrics

**用途**：F10 — Prometheus 指标端点，返回 Prometheus 格式的监控指标。

**鉴权**：不需要（公开端点，但建议通过网关/IP 白名单保护）

**请求参数**：无

**成功响应 (200)**：
响应体为纯文本（`Content-Type: text/plain; version=0.0.4; charset=utf-8`），包含以下指标：

```
# HELP gateway_http_requests_total Total number of HTTP requests
# TYPE gateway_http_requests_total counter
gateway_http_requests_total{method="POST",endpoint="/v1/chat/completions",status="200"} 15000
gateway_http_requests_total{method="POST",endpoint="/v1/chat/completions",status="401"} 50
gateway_http_requests_total{method="POST",endpoint="/v1/chat/completions",status="429"} 120
gateway_http_requests_total{method="GET",endpoint="/v1/models",status="200"} 3000

# HELP gateway_request_duration_seconds Request duration in seconds
# TYPE gateway_request_duration_seconds histogram
gateway_request_duration_seconds_bucket{endpoint="/v1/chat/completions",le="0.1"} 500
gateway_request_duration_seconds_bucket{endpoint="/v1/chat/completions",le="0.5"} 3000
gateway_request_duration_seconds_bucket{endpoint="/v1/chat/completions",le="1.0"} 4500
gateway_request_duration_seconds_bucket{endpoint="/v1/chat/completions",le="+Inf"} 5000
gateway_request_duration_seconds_sum{endpoint="/v1/chat/completions"} 1200.5
gateway_request_duration_seconds_count{endpoint="/v1/chat/completions"} 5000

# HELP gateway_cache_hits_total Total cache hits by tier
# TYPE gateway_cache_hits_total counter
gateway_cache_hits_total{tier="L1"} 8000
gateway_cache_hits_total{tier="L2"} 3000
gateway_cache_hits_total{tier="L3"} 1500

# HELP gateway_cache_misses_total Total cache misses
# TYPE gateway_cache_misses_total counter
gateway_cache_misses_total 2500

# HELP gateway_tokens_total Total tokens processed
# TYPE gateway_tokens_total counter
gateway_tokens_total{type="prompt"} 5000000
gateway_tokens_total{type="completion"} 2000000

# HELP gateway_cost_total Total cost in USD
# TYPE gateway_cost_total gauge
gateway_cost_total 125.50

# HELP gateway_cost_by_model Total cost by model
# TYPE gateway_cost_by_model counter
gateway_cost_by_model{model="gpt-4o"} 80.00
gateway_cost_by_model{model="claude-3-5-sonnet"} 45.50

# HELP gateway_circuit_breaker_state Circuit breaker state per provider
# TYPE gateway_circuit_breaker_state gauge
gateway_circuit_breaker_state{provider="openai"} 0
gateway_circuit_breaker_state{provider="anthropic"} 0
# 0=CLOSED, 1=OPEN, 2=HALF-OPEN

# HELP gateway_active_requests Currently active requests
# TYPE gateway_active_requests gauge
gateway_active_requests 12

# HELP gateway_up Whether the gateway is healthy
# TYPE gateway_up gauge
gateway_up 1
```

**错误响应**：

| HTTP 状态码 | error.code | error.message | 触发条件 |
|------------|-----------|--------------|---------|
| 500 | `internal_error` | "Failed to collect metrics" | 指标采集失败 |

---

### GET /health

**用途**：基础设施健康检查端点，用于负载均衡器探活和服务状态监控。

**鉴权**：不需要

**请求参数**：无

**成功响应 (200)**：
```json
{
  "data": {
    "status": "healthy",                     // 字符串: "healthy" | "degraded" | "unhealthy"
    "version": "1.0.0",                      // 字符串，Gateway 版本
    "uptime_seconds": 86400,                 // 整数，运行时间（秒）
    "timestamp": "2024-01-21T10:00:00Z",      // 字符串，ISO 8601 当前时间
    "dependencies": {                         // 对象，各依赖服务健康状态
      "redis": {
        "status": "connected",                // 字符串: "connected" | "disconnected" | "error"
        "latency_ms": 0.5                    // 浮点数，最近一次 ping 延迟（毫秒）
      },
      "qdrant": {
        "status": "connected",
        "latency_ms": 2.3
      },
      "prometheus": {
        "status": "connected",
        "latency_ms": 1.0
      }
    },
    "plugins": {                              // 对象，各插件状态
      "prompt_compress": {
        "enabled": true,                      // 布尔值
        "status": "healthy"                   // 字符串: "healthy" | "degraded" | "error"
      },
      "prompt_cache": {
        "enabled": true,
        "status": "healthy"
      },
      "semantic_cache": {
        "enabled": true,
        "status": "healthy"
      },
      "model_router": {
        "enabled": true,
        "status": "healthy"
      },
      "pii_detector": {
        "enabled": true,
        "status": "healthy"
      }
    }
  },
  "message": "success"
}
```

**降级响应 (200)** — 当部分依赖不可用时：
```json
{
  "data": {
    "status": "degraded",
    "version": "1.0.0",
    "uptime_seconds": 86400,
    "timestamp": "2024-01-21T10:00:00Z",
    "dependencies": {
      "redis": {
        "status": "error",
        "latency_ms": 0
      },
      "qdrant": {
        "status": "connected",
        "latency_ms": 2.3
      },
      "prometheus": {
        "status": "connected",
        "latency_ms": 1.0
      }
    },
    "plugins": {
      "prompt_cache": {
        "enabled": true,
        "status": "degraded"
      },
      "semantic_cache": {
        "enabled": true,
        "status": "degraded"
      }
    }
  },
  "message": "partial degradation"
}
```

**错误响应**：

| HTTP 状态码 | error.code | error.message | 触发条件 |
|------------|-----------|--------------|---------|
| 503 | `service_unavailable` | "Gateway is not ready" | 服务正在启动中或所有关键依赖不可用 |
