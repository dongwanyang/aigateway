# 数据库 Schema
> 数据库: Redis 7 (KV 缓存 + Pub/Sub) + Qdrant 1.7 (向量存储)
> 字符集: N/A (Redis 使用二进制安全字符串，Qdrant 使用 UTF-8)
> 排序规则: N/A

## 命名规范
- Redis Key: 冒号分隔的命名空间前缀（如 `aigateway:key:{hash}`）
- Qdrant Collection: snake_case（如 `semantic_cache`、`rag_documents`）
- Qdrant Payload 字段: snake_case
- Pub/Sub Channel: 命名空间前缀 + 功能描述（如 `aigateway:keys:sync`）

---

## Redis Key 结构

### 1. API Key 存储

**用途**：F05 — 存储所有 API Key 及其配额、状态信息，支持分布式多实例查询和 Pub/Sub 同步。

**Key 格式**：`aigateway:key:{key_hash}`
- `key_hash`: API Key 值的 SHA-256 哈希（取前 16 位 hex 字符串）

**存储类型**：Hash

**字段定义**：

| 字段名 | 类型 | 约束 | 说明 |
|--------|------|------|------|
| `key_id` | string | NOT NULL | API Key 内部 ID（如 `key_abc123`） |
| `key_prefix` | string | NOT NULL | Key 前 8 字符，用于展示和识别 |
| `user_id` | string | NOT NULL | 关联的用户 ID |
| `group_id` | string | NOT NULL | 所属用户组 ID（如 `grp-admin-team`），无组时为 `grp-default` |
| `cache_scope` | string | NOT NULL, 枚举 | `"private"` \| `"group"` \| `"public"`，决定缓存共享范围 |
| `status` | string | NOT NULL, 枚举 | `"active"` \| `"revoked"` \| `"suspended"` |
| `created_at` | string | NOT NULL | ISO 8601 创建时间 |
| `last_used_at` | string | NULL | ISO 8601 最后使用时间 |
| `daily_tokens_limit` | integer | NOT NULL | 每日 token 上限 |
| `daily_tokens_used` | integer | NOT NULL, 默认 0 | 今日已用 token 数 |
| `monthly_cost_limit` | float | NOT NULL | 每月成本上限（美元） |
| `monthly_cost_used` | float | NOT NULL, 默认 0.0 | 本月已用成本（美元） |
| `rate_limit_rpm` | integer | NOT NULL | 每分钟请求数上限 |
| `rate_limit_tpm` | integer | NOT NULL | 每分钟 token 数上限 |
| `rpm_window_start` | integer | NOT NULL | RPM 窗口起始 Unix 时间戳 |
| `rpm_window_count` | integer | NOT NULL, 默认 0 | 当前 RPM 窗口内的请求数 |
| `tpm_window_start` | integer | NOT NULL | TPM 窗口起始 Unix 时间戳 |
| `tpm_window_count` | integer | NOT NULL, 默认 0 | 当前 TPM 窗口内的 token 数 |

**TTL**：永不过期（Key 生命周期由管理接口控制）

**索引/查找方式**：
- 主查找：通过 `aigateway:key_lookup:{key_prefix}` -> `key_hash` 反向查找
- 前缀扫描：`SCAN 0 aigateway:key:*` 遍历所有 Key

**Key 格式**：`aigateway:key_lookup:{key_prefix}`
**存储类型**：String
**值**：`key_hash`
**TTL**：与对应 Key 一致

---

### 1b. 用户组存储

**用途**：F05 — 存储用户组及其组级配额、成员集合。组级配额是成员共享池，个人配额是组内子限额。

**Key 格式**：`aigateway:group:{group_id}`
- `group_id`：组 ID（如 `grp-admin-team`），由 `slugify(name)` 生成

**存储类型**：Hash

**字段定义**：

| 字段名 | 类型 | 约束 | 说明 |
|--------|------|------|------|
| `name` | string | NOT NULL | 组名称 |
| `status` | string | NOT NULL, 枚举 | `"active"` \| `"suspended"` |
| `created_at` | string | NOT NULL | ISO 8601 创建时间 |
| `updated_at` | string | NOT NULL | ISO 8601 更新时间 |
| `daily_tokens_limit` | integer | NOT NULL | 组每日 token 上限 |
| `daily_tokens_used` | integer | NOT NULL, 默认 0 | 组今日已用 token 数 |
| `monthly_cost_limit` | float | NOT NULL | 组每月成本上限（美元） |
| `monthly_cost_used` | float | NOT NULL, 默认 0.0 | 组本月已用成本（美元） |
| `rate_limit_rpm` | integer | NOT NULL | 组 RPM 上限 |
| `rate_limit_tpm` | integer | NOT NULL | 组 TPM 上限 |
| `rpm_window_start` | integer | NOT NULL | RPM 窗口起始 Unix 时间戳 |
| `rpm_window_count` | integer | NOT NULL, 默认 0 | 当前 RPM 窗口内的请求数 |
| `tpm_window_start` | integer | NOT NULL | TPM 窗口起始 Unix 时间戳 |
| `tpm_window_count` | integer | NOT NULL, 默认 0 | 当前 TPM 窗口内的 token 数 |

**TTL**：永不过期

**Key 格式**：`aigateway:group_lookup:{name}`
**存储类型**：String
**值**：`group_id`
**TTL**：与对应组一致

**Key 格式**：`aigateway:group:{group_id}:members`
**存储类型**：Set
**值**：`key_hash` 集合（组成员的 Key Hash）
**TTL**：与对应组一致

**Key 格式**：`aigateway:groups:index`
**存储类型**：Set
**值**：所有 `group_id`
**TTL**：永不过期

**Pub/Sub 频道**：`aigateway:groups:sync`
- 消息格式：`{"event_type": "group_created"|"group_updated"|"group_deleted", "group_id": "...", "name": "...", "timestamp": "..."}`

**配额计数**：`aigateway:quota:{group_id}:daily:{YYYY-MM-DD}` 和 `aigateway:quota:{group_id}:monthly:{YYYY-MM}`
- 与 API Key 配额结构相同（`tokens_in`, `tokens_out`, `cost_usd`, `request_count`, `model_usage`）

---

### 2. 配额计数（API Key 维度）

**用途**：F05 — 按日/按月记录每个 API Key 的 token 消耗和成本，用于配额检查和软告警。

**Key 格式**：`aigateway:quota:{key_hash}:{period}`
- `period`：`daily:{YYYY-MM-DD}` 或 `monthly:{YYYY-MM}`

**存储类型**：Hash

**字段定义**：

| 字段名 | 类型 | 约束 | 说明 |
|--------|------|------|------|
| `tokens_in` | integer | NOT NULL, 默认 0 | 当日/月输入 token 累计 |
| `tokens_out` | integer | NOT NULL, 默认 0 | 当日/月输出 token 累计 |
| `cost_usd` | float | NOT NULL, 默认 0.0 | 当日/月成本累计（美元） |
| `request_count` | integer | NOT NULL, 默认 0 | 当日/月请求总数 |
| `model_usage` | string | NULL | JSON 字符串，各模型 token 分布 `{ "gpt-4o": { "in": 100, "out": 200 } }` |

**TTL**：
- `daily` 键：当日 23:59:59 UTC 自动过期
- `monthly` 键：当月最后一天 23:59:59 UTC 自动过期

**软告警 Key**：`aigateway:alert:{key_hash}:{type}`
- `type`：`daily_token_80` | `monthly_cost_80` | `rpm_80` | `tpm_80`
- **存储类型**：String
- **值**：`"triggered"` 或 `"acknowledged"`
- **TTL**：300 秒（5 分钟去重，避免同一阈值重复告警）

---

### 3. 缓存键

#### L1 缓存（进程内）
**存储类型**：`cachetools.LRUCache`（Python 内存对象，非 Redis）

**Key 生成**：`SHA-256(normalized_prompt + model + temperature + max_tokens + top_p + user_id)`
- `normalized_prompt`：去除空白差异后的 messages 序列化字符串
- 缓存 Value：完整 OpenAI 格式响应 JSON 字符串

**容量**：默认 1000 条目，LRU 淘汰

#### L2 缓存（Redis KV）
**用途**：F03 — 分布式 KV 缓存，精确匹配缓存。

**Key 格式**：`aigateway:cache:v2:{cache_key_hash}`
- `cache_key_hash`：64 位 hex SHA-256

**v2 vs v1 (2026-07-06 起)**：`v1:` 前缀已废弃(TTL 自然到期后消亡,不清理),新数据全部写 `v2:`。v2 分层设计如下,由 `CacheManager.generate_cache_key()` 统一生成:

**v2 cache_key_hash 生成规则**:
```
SHA-256("v2" | pipeline_kind | model_family | temp_bucket | mt_bucket
        [ | u=user_id if scope=private ]
        [ | g=group_id if scope=group ] | normalized_prompt)
```

| 段 | 说明 |
|---|---|
| `v2` | Schema 版本前缀,方便未来 v3 平滑升级 |
| `pipeline_kind` | `understanding` \| `generation`,强制隔离两条管道,防止跨管道结果污染 |
| `model_family` | 从 `model` 抽取,去掉尾部日期 snapshot(如 `gpt-4o-2024-08-06` → `gpt-4o`),同 family 不同 snapshot 共享缓存;`model=='auto'` 保留原样 |
| `temp_bucket` | temperature 分桶:`exact_zero`(<=0.05) / `det`(<=0.3) / `bal`(<=0.9) / `cre`(>0.9) |
| `mt_bucket` | max_tokens 分桶:`any`(None/0) / `le_256` / `le_512` / `le_1024` / `le_2048` / `le_4096` / `le_8192` / `le_16384` / `gt_16384` |
| `u=user_id` | 仅当 `cache_scope=private` 时纳入 |
| `g=group_id` | 仅当 `cache_scope=group` 时纳入,组内成员共享缓存 |
| `normalized_prompt` | dispatcher 已用 `_extract_cacheable_context(messages, tail=3)` 只保留 system + 末尾 3 轮对话,并经 `_normalize_prompt`(NFKC + 空白折叠)处理 |

**⚠️ 明确忽略的字段**:`top_p`(实践中几乎全 1.0,分桶收益极小,反而拉高 MISS 率)。

**cache_scope 三档**:
- `private`：仅当前用户共享（PII 命中自动升 private，或显式 `X-Cache-Scope: private`）
- `group`（默认）：同组内所有 key 共享（优先级：header `X-Cache-Scope: group|public` > 默认 group）
- `public`：全局共享，无 user/group 标识

**cache_scope 决策优先级**(见 dispatcher `_resolve_cache_scope`):
1. 显式请求头 `X-Cache-Scope: private|group|public`
2. PII 检测命中 → 强制 `private`
3. 默认 `group`

**存储类型**：String（压缩后的 JSON 字节）

**字段定义**（Value 结构）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `response_json` | string | 压缩后的完整 OpenAI 响应 JSON 字节串 |
| `prompt_hash` | string | 输入 prompt 的 SHA-256 |
| `model` | string | 生成该响应的模型 |
| `created_at` | integer | Unix 时间戳（秒） |
| `token_count` | integer | 响应 token 数 |
| `hit_count` | integer | 命中次数（用于 LRU 淘汰优先级） |

**TTL**：可配置，默认 3600 秒（1 小时），由 `prompt_cache.config.ttl` 决定

**压缩**：LZ4 压缩存储，节省 Redis 内存

#### L3 缓存（Qdrant 向量）
**用途**：F03 — 语义缓存，向量相似度匹配。存储在 Qdrant Collection 中，详见下文 Qdrant 结构。

---

### 4. Pub/Sub 频道

**用途**：F05 — 多实例部署时，API Key 变更事件的广播通道。

**频道列表**：

| 频道名 | 类型 | 订阅者 | 消息格式 | 触发场景 |
|--------|------|--------|---------|---------|
| `aigateway:keys:sync` | String | 所有 Gateway 实例 | JSON | API Key 创建/撤销/更新 |
| `aigateway:groups:sync` | String | 所有 Gateway 实例 | JSON | 用户组创建/更新/删除/成员变更 |
| `aigateway:config:reload` | String | 所有 Gateway 实例 | JSON | 配置热加载通知 |

**消息格式** (`aigateway:keys:sync`)：
```json
{
  "event_type": "key_created" | "key_revoked" | "key_updated",  // 字符串，事件类型
  "key_id": "key_abc123",                                        // 字符串，API Key ID
  "user_id": "dev-user",                                         // 字符串，用户 ID
  "timestamp": "2024-01-21T10:00:00Z"                            // 字符串，ISO 8601 时间戳
}
```

**消息格式** (`aigateway:config:reload`)：
```json
{
  "event_type": "config_reload",                                   // 字符串，固定值
  "config_version": "v1.2",                                        // 字符串，新版本配置标识
  "timestamp": "2024-01-21T10:00:00Z"                              // 字符串，ISO 8601 时间戳
}
```

---

### 5. 速率限制窗口

**用途**：F05 — 滑动窗口速率限制，存储每个 API Key 的最近请求时间戳。

**Key 格式**：`aigateway:ratelimit:{key_hash}:rpm`
**存储类型**：Sorted Set

**字段定义**：

| 字段 | 类型 | 说明 |
|------|------|------|
| member | string | 请求 ID（UUID），用于去重 |
| score | float | Unix 时间戳（秒），请求发生时间 |

**TTL**：120 秒（窗口大小 + 余量）

**Key 格式**：`aigateway:ratelimit:{key_hash}:tpm`
**存储类型**：String
**值**：当前窗口内累计 token 数
**TTL**：60 秒

---

## Qdrant Collection 结构

### 1. 语义缓存集合

**用途**：F03 — 存储 prompt 的嵌入向量和缓存响应，用于语义相似度匹配（L3 缓存）。

**Collection 名称**：`semantic_cache`

**向量配置**：
| 参数 | 值 | 说明 |
|------|-----|------|
| distance | COSINE | 余弦相似度，适合语义比较 |
| size | 384 | 向量维度（all-MiniLM-L6-v2 输出维度） |
| hnsw_config.m | 16 | HNSW 图参数 |
| hnsw_config.ef_construct | 128 | HNSW 构建参数 |
| optimization_config.memlock | false | 是否内存锁定 |

**Payload Schema**：

| 字段名 | 类型 | 约束 | 索引类型 | 说明 |
|--------|------|------|---------|------|
| `prompt_hash` | string | NOT NULL | Keyword (精确匹配) | 输入 prompt 的 SHA-256 哈希 |
| `prompt_normalized` | string | NOT NULL | Keyword (全文检索) | 归一化后的 prompt 文本 |
| `model` | string | NOT NULL | Keyword | 生成响应的模型名称 |
| `response_json` | string | NOT NULL | — | 完整 OpenAI 格式响应 JSON 字符串 |
| `user_id` | string | NOT NULL | Keyword | 所属用户 ID（用于多租户隔离） |
| `created_at` | integer | NOT NULL | Integer | Unix 时间戳（秒） |
| `ttl` | integer | NOT NULL | Integer | 过期时间戳（Unix 秒），用于定期清理 |
| `hit_count` | integer | NOT NULL, 默认 0 | Integer | 命中次数（用于淘汰策略） |
| `token_count` | integer | NOT NULL | Integer | 响应 token 数 |
| `cache_tier` | string | NOT NULL | Keyword | 固定值 `"L3"` |
| `embedding_model` | string | NOT NULL | Keyword | 用于生成此向量的嵌入模型名 |

**TTL 清理策略**：
- Qdrant 原生 TTL 过滤器：`ttl > now()` 的向量将被视为过期
- 建议每 6 小时运行一次清理任务，删除 `ttl < now()` 的向量

**查询参数**：
| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `vector` | float[] | 是 | — | 归一化 prompt 的嵌入向量 |
| `limit` | integer | 否 | 1 | 返回最相似的结果数 |
| `score_threshold` | float | 否 | 0.95 | 最低相似度阈值（余弦相似度） |
| `payload_fields` | string[] | 否 | 全部 | 需要返回的 payload 字段列表 |

---

### 2. RAG 文档集合（预留，MVP 不启用）

**用途**：F01 预留 — 存储用户上传文档的嵌入向量，用于 RAG 检索。MVP 阶段管道预留位置，实际检索引擎延后。

**Collection 名称**：`rag_documents`

**向量配置**：同 `semantic_cache`

**Payload Schema**：

| 字段名 | 类型 | 约束 | 索引类型 | 说明 |
|--------|------|------|---------|------|
| `document_id` | string | NOT NULL | Keyword | 文档唯一 ID（UUID） |
| `user_id` | string | NOT NULL | Keyword | 所属用户 ID |
| `filename` | string | NOT NULL | Keyword | 原始文件名 |
| `file_type` | string | NOT NULL | Keyword | 文件类型: `pdf` \| `txt` \| `csv` \| `json` \| `markdown` |
| `chunk_index` | integer | NOT NULL | Integer | 文档内分块索引 |
| `chunk_text` | string | NOT NULL | Keyword (全文) | 分块文本内容 |
| `metadata` | object | NULL | — | JSON 对象，附加元数据（来源、页码等） |
| `created_at` | integer | NOT NULL | Integer | Unix 时间戳（秒） |
| `deleted` | boolean | NOT NULL, 默认 false | Keyword | 软删除标记 |

---

## In-Memory 数据结构

### 1. L1 缓存 (LRUCache)

**位置**：`aigateway_core/caching.py` — `LRUCache` 实例

**配置**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `maxsize` | 1000 | 最大缓存条目数 |
| `eviction_policy` | LRU | 淘汰策略：最近最少使用 |
| `key_generator` | SHA-256(prompt_hash + model + params) | 缓存键生成函数 |
| `value_serializer` | JSON | 序列化方式 |
| `thread_safe` | true | 线程安全（使用 threading.Lock） |

**Key 结构**：`SHA-256(normalized_messages_json + model_name + temperature + max_tokens + top_p + user_id)`

**Value 结构**：完整 OpenAI `/v1/chat/completions` 响应 JSON 字符串

**TTL**：无（由 LRU 淘汰控制）

**命中率指标**：`gateway_cache_hits_total{tier="L1"}`

---

### 2. 插件管线上下文 (PipelineContext)

**位置**：`aigateway_core/context.py`

**字段定义**：

| 字段名 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `request` | dict | — | 原始 OpenAI 格式请求体 |
| `response` | Optional[str] | None | 缓存命中时设置的响应内容 |
| `should_stop` | bool | False | 短路标记，True 时跳过后续插件 |
| `should_stream` | bool | False | 是否流式响应 |
| `trace_id` | str | UUID4() | OpenTelemetry 追踪 ID |
| `request_id` | str | UUID4() | 唯一请求 ID |
| `user_id` | Optional[str] | None | 从 API Key 解析的用户 ID |
| `extra` | dict | {} | 插件间传递的命名空间数据 |

**extra 命名空间约定**：

| 命名空间 | 字段 | 类型 | 说明 |
|---------|------|------|------|
| `prompt_compress` | `original_length` | int | 原始 prompt 长度 |
| `prompt_compress` | `compressed_prompt` | str | 压缩后的 prompt 文本 |
| `prompt_compress` | `compression_ratio` | float | 压缩比例 |
| `prompt_cache` | `cache_key` | str | L1/L2 缓存键 |
| `prompt_cache` | `cache_hit` | bool | 是否命中缓存 |
| `semantic_cache` | `similarity_score` | float | 语义相似度得分 |
| `semantic_cache` | `cached_response` | str | 缓存的响应内容 |
| `semantic_cache` | `collection` | str | Qdrant 集合名 |
| `pii_detector` | `detected_categories` | list[str] | 检测到的 PII 类别 |
| `pii_detector` | `sanitized_prompt` | str | 脱敏后的 prompt |
| `model_router` | `selected_provider` | str | 选中的提供商 |
| `model_router` | `selected_model` | str | 选中的模型 |
| `model_router` | `fallback_chain` | list[str] | 经历的降级链 |
| `model_router` | `circuit_breaker_state` | str | 熔断器状态 |

---

### 3. 熔断器状态 (CircuitBreaker)

**位置**：`aigateway_core/circuit_breaker.py` — per-provider 实例

**状态枚举**：
| 状态 | 整数值 | 说明 |
|------|--------|------|
| `CLOSED` | 0 | 正常操作 |
| `OPEN` | 1 | 拒绝所有请求，立即触发降级 |
| `HALF-OPEN` | 2 | 放行一个探测请求 |

**每提供商配置**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `failure_threshold` | 5 | 连续失败次数阈值 |
| `recovery_timeout` | 60 | HALF-OPEN 等待时间（秒） |
| `expected_exception` | litellm.BadRequestError | 触发熔断的异常类型 |
| `last_failure_time` | Unix timestamp | 最后一次失败时间 |
| `failure_count` | 0 | 当前连续失败次数 |

**受影响的提供商枚举**：`openai`、`anthropic`、`gemini`、`bedrock`、`ollama`

---

### 4. 请求计数器 (用于速率限制)

**位置**：`aigateway_core/security.py` — 内存中的滑动窗口计数器

**结构**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `window_start` | int | 当前时间窗口起始时间戳 |
| `counter` | int | 当前窗口内的计数 |
| `token_counter` | int | 当前窗口内的 token 累计 |

**窗口大小**：RPM 窗口 60 秒，TPM 窗口 60 秒

---

## ER 关系图

```
API Keys (Redis Hash)
  ├── 1:N Quota Records (Redis Hash, daily/monthly)
  ├── 1:N Rate Limit Windows (Redis SortedSet/String)
  ├── 1:N Cache Entries (Redis String + Qdrant Vector)
  └── N:1 User Group (via group_id → aigateway:group:{group_id})

User Groups (Redis Hash + Set)
  ├── aigateway:group:{group_id} — group hash (limits, used, rpm/tpm windows)
  ├── aigateway:group_lookup:{name} — name → group_id
  ├── aigateway:group:{group_id}:members — Set<key_hash>
  ├── aigateway:groups:index — Set<group_id>
  ├── 1:N Quota Records (Redis Hash, daily/monthly)
  └── 1:N API Keys (via group_id foreign reference)

Qdrant Collections
  ├── semantic_cache (vector + payload)
  └── rag_documents (vector + payload, reserved)

Pub/Sub Channels
  ├── aigateway:keys:sync (broadcast to all gateway instances)
  ├── aigateway:groups:sync (broadcast to all gateway instances)
  └── aigateway:config:reload (broadcast to all gateway instances)
```
