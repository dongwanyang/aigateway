# Requirements Document

## Introduction

本文档定义 AI Gateway 项目在架构、技术选型、用户体验和配置安全方面的优化需求。目标是提升系统的生产可用性、可扩展性、安全性和开发者体验，同时保持向后兼容。

基于用户反馈，重点解决以下问题：
- 多 Worker 部署时 Prometheus 指标不共享导致数据不准
- .env 和 config.yaml 配置重复且职责不清
- Redis 清空后系统无法自恢复
- 控制面板 UX 改进（删除即移除、延迟准确性、用户维度成本、trace_id 可点击）

## Glossary

- **Gateway**: AI Gateway 的 FastAPI 后端服务，提供 OpenAI 兼容 API 代理
- **Control_Panel**: 基于 React + Vite 构建的 Web 管理控制面板
- **Pipeline_Engine**: 异步插件管线引擎，按拓扑顺序执行插件链
- **Cache_Manager**: 三级缓存管理器（L1 LRU → L2 Redis → L3 Qdrant）
- **Config_Manager**: YAML 配置加载器，支持环境变量覆盖和 Watchdog 热重载
- **Key_Store**: API Key 存储与配额管理器，基于 Redis Hash
- **Circuit_Breaker**: 各下游 LLM 提供商的熔断器组件
- **LiteLLM_Bridge**: LiteLLM 库封装层，提供多模型路由和 fallback
- **Metrics_Collector**: Prometheus 指标收集器单例
- **Rate_Limiter**: 请求速率限制组件（RPM/TPM 维度）
- **Config_Validator**: 配置文件结构和语义验证组件

## Requirements

### Requirement 1: Multi-Worker Prometheus 指标共享

**User Story:** As a 运维工程师, I want 多 worker 部署时 Prometheus 指标在所有 worker 间正确共享和聚合, so that 监控数据准确反映整个服务的真实状态。

**背景:** 当前每个 uvicorn worker 独立执行 lifespan，各自创建独立的 MetricsCollector 和 CollectorRegistry，导致 Counter 值只反映单个 worker 的数据，Gauge 值被最后写入的 worker 覆盖。

#### Acceptance Criteria

1. WHEN server.workers > 1 时, THE Gateway SHALL 使用 Prometheus multiprocess 模式，将每个 worker 的指标写入共享目录（PROMETHEUS_MULTIPROC_DIR）
2. THE Gateway SHALL 在容器启动脚本中创建并清空 PROMETHEUS_MULTIPROC_DIR 临时目录，确保每次重启指标归零
3. WHEN /metrics 端点被请求时, THE Gateway SHALL 使用 prometheus_client.multiprocess.MultiProcessCollector 合并所有 worker 的指标文件后返回
4. WHILE 多 worker 模式运行时, THE Metrics_Collector SHALL 将 gateway_cost_total 从 Gauge 改为 Counter 类型（Counter 在 multiprocess 模式下正确累加，Gauge 会被覆盖）
5. WHILE 单 worker 模式运行时, THE Gateway SHALL 使用当前的独立 CollectorRegistry 行为保持不变
6. THE Gateway SHALL 在 docker-compose.yml 中使用 gunicorn + UvicornWorker 启动，workers 数量通过环境变量 AI_GATEWAY_WORKERS 配置，默认为 1

### Requirement 2: 统一配置源 — 废除 .env 依赖

**User Story:** As a 开发者, I want 只维护一个配置文件 config.yaml, so that 不需要在 .env 和 config.yaml 之间来回切换。

**背景:** 用户反馈 .env 和 config.yaml 有重复内容（如 Redis URL、API Key），不确定每次该改哪个文件。

#### Acceptance Criteria

1. THE Gateway SHALL 以 config.yaml 作为唯一配置源，从中读取所有配置项（包括之前放在 .env 中的 Redis URL、Qdrant URL 等）
2. THE config.yaml SHALL 新增顶层 `infrastructure` 段，包含 redis_url、qdrant_url、prometheus_enabled 等基础设施配置
3. THE Gateway SHALL 仍支持环境变量 override（如 AI_GATEWAY_REDIS_URL 覆盖 config.yaml 中的 infrastructure.redis_url），但 .env 文件不再是必需项
4. THE Gateway SHALL 在 README 中明确说明：config.yaml 是唯一配置文件，环境变量仅用于 Docker/K8s 场景的 override
5. THE Control_Panel SHALL 提供"系统配置"页面，允许管理员通过 Web 界面编辑 config.yaml 的关键配置项（providers、plugins、infrastructure）
6. WHEN 通过 Control_Panel 修改配置时, THE Gateway SHALL 将变更写回 config.yaml 并触发热重载

### Requirement 3: Redis 清空后自动恢复

**User Story:** As a 运维工程师, I want Redis 被清空后系统能自动恢复正常功能, so that 不需要手动干预即可重新添加 API Key。

**背景:** 用户将 Redis 中所有 key 删除后，控制台无法正常添加新 API Key（因为 seed_from_config 只在启动时运行一次）。

#### Acceptance Criteria

1. WHEN 任何 admin API 端点检测到 Key_Store 中无任何 API Key 时, THE Gateway SHALL 自动从 config.yaml auth.api_keys 重新导入种子 Key
2. THE Key_Store SHALL 在每次 create/list 操作前检查 Redis 连接状态，如果 Redis 已连接但关键数据不存在则触发重新种子
3. WHEN 重新种子完成后, THE Gateway SHALL 记录 INFO 日志 "API Keys re-seeded from config.yaml"
4. THE Key_Store SHALL 实现幂等的 seed_from_config 方法 — 如果 Key 已存在则跳过，不会创建重复条目
5. WHEN Redis 完全不可用时, THE Gateway SHALL 返回清晰的错误消息 "Redis connection required for key management" 而非抛出未捕获异常

### Requirement 4: 配额管理 — 删除后从列表移除

**User Story:** As a 控制面板用户, I want 撤销的 API Key 从列表中完全消失, so that 列表只显示有意义的活跃 Key。

#### Acceptance Criteria

1. WHEN 用户在 Control_Panel 中撤销一个 API Key 时, THE Control_Panel SHALL 立即从当前列表中移除该 Key（乐观更新）
2. THE Backend DELETE /admin/api-keys/{key_id} SHALL 从 Redis 中完全删除该 Key 的所有数据（而非仅标记 status=revoked）
3. THE Control_Panel 列表 SHALL 默认只显示 status=active 的 API Key
4. IF 后端删除操作失败, THEN THE Control_Panel SHALL 回滚乐观更新并显示错误提示

### Requirement 5: 概览页面 — 平均延迟准确测量

**User Story:** As a 运维工程师, I want 概览页面显示准确的平均延迟, so that 能正确评估系统性能。

**背景:** 当前请求日志中 duration_ms 始终记录为 0，因为 `_record_request_log` 被调用时没有传入实际的请求耗时。

#### Acceptance Criteria

1. THE Gateway SHALL 在请求开始时记录 start_time，在响应返回前计算实际耗时 duration_ms = (time.time() - start_time) * 1000
2. THE Gateway SHALL 将准确的 duration_ms 传递给 _record_request_log 函数和 Prometheus histogram
3. WHEN 缓存命中时, THE Gateway SHALL 记录实际的缓存查找耗时（通常 < 5ms）而非硬编码 0
4. THE Control_Panel 概览页面 SHALL 从 Prometheus histogram 的 sum/count 计算准确的平均延迟
5. THE Control_Panel SHALL 在无请求数据时显示 "—" 而非 "0ms"

### Requirement 6: 概览页面 — 成本按用户维度统计

**User Story:** As a 管理员, I want 概览页面展示按用户维度的成本分布, so that 能快速识别高消费用户并合理分配预算。

**背景:** 概览页面的"成本分布 by 模型"图表和成本分析页面重复。改为按用户维度可提供差异化视角。

#### Acceptance Criteria

1. THE Metrics_Collector SHALL 新增 gateway_cost_by_user_total Counter 指标，标签为 user_id
2. THE Gateway SHALL 在每次计费请求完成后，同时递增 gateway_cost_by_model_total 和 gateway_cost_by_user_total
3. THE Control_Panel 概览页面 SHALL 将"成本分布 by 模型"图表替换为"成本分布 by 用户"图表
4. THE Control_Panel 概览页面的用户成本图表 SHALL 展示 Top 5 用户的成本条形图
5. WHEN 用户数少于 2 时, THE Control_Panel SHALL 显示占位文案 "数据不足，等待更多请求..."

### Requirement 7: 日志页面 — trace_id 可点击查看详情

**User Story:** As a 开发者, I want 点击 trace_id 能查看该 trace 下的所有关联请求, so that 能追踪一次完整请求的上下文。

#### Acceptance Criteria

1. WHEN 用户点击日志表格中的 trace_id 时, THE Control_Panel SHALL 展开该行下方的详情面板
2. THE 详情面板 SHALL 展示该请求的完整信息：完整 request_id、完整 trace_id、user_id、模型、状态码、延迟、缓存命中状态、时间戳
3. THE Control_Panel SHALL 通过搜索过滤功能支持按 trace_id 查看同一 trace 下的所有请求
4. WHEN 点击已展开的 trace_id 时, THE Control_Panel SHALL 折叠详情面板
5. THE 详情面板 SHALL 使用动画过渡展开/折叠（200ms ease-in-out）

### Requirement 8: CORS 中间件配置

**User Story:** As a 前端开发者, I want Gateway 正确配置 CORS, so that Control_Panel 能从不同域名安全访问 API。

#### Acceptance Criteria

1. THE Gateway SHALL 在应用初始化时添加 CORSMiddleware
2. WHEN config.yaml 中 server.cors_origins 已配置时, THE Gateway SHALL 使用其值作为允许的来源列表
3. WHILE cors_origins 未配置时, THE Gateway SHALL 使用默认值 ["http://localhost:3000", "http://localhost:5173"] 作为允许来源
4. THE Gateway SHALL 允许 GET、POST、PUT、DELETE、OPTIONS 方法和 Authorization、Content-Type、X-API-Key 请求头

### Requirement 9: 全局速率限制中间件

**User Story:** As a 系统管理员, I want 所有 API 端点受速率限制保护, so that 管理端点不会被恶意请求滥用。

#### Acceptance Criteria

1. THE Rate_Limiter SHALL 对所有 /admin/* 端点实施全局速率限制
2. WHEN 同一 IP 在 60 秒窗口内对 /admin/* 端点发送超过 30 次请求时, THE Rate_Limiter SHALL 返回 HTTP 429 响应
3. THE Rate_Limiter SHALL 在 429 响应中包含 Retry-After 头，值为窗口剩余秒数
4. THE Rate_Limiter SHALL 对 /health 和 /metrics 端点豁免速率限制
5. WHEN Redis 不可用时, THE Rate_Limiter SHALL 降级为进程内计数器实现速率限制

### Requirement 10: 配置验证与安全

**User Story:** As a 开发者, I want 配置文件在加载时被验证, so that 拼写错误或无效值能被及时发现。

#### Acceptance Criteria

1. WHEN config.yaml 被加载时, THE Config_Validator SHALL 验证所有顶层字段名在允许集合内
2. WHEN config.yaml 包含未识别的顶层字段时, THE Config_Validator SHALL 记录 WARNING 日志
3. WHEN providers 配置中 api_key 以 "sk-" 开头且长度 > 10 时, THE Config_Validator SHALL 记录 WARNING 提示使用 ${ENV_VAR} 语法
4. THE Config_Manager SHALL 支持 api_key 字段使用 ${ENV_VAR_NAME} 语法引用环境变量
5. THE Config_Validator SHALL 在验证失败时仍加载配置（宽容模式），仅通过日志通知

### Requirement 11: 错误信息标准化

**User Story:** As a API 使用者, I want API 返回标准化且安全的错误信息, so that 调试方便但不暴露内部实现细节。

#### Acceptance Criteria

1. THE Gateway SHALL 对所有 4xx 和 5xx 响应使用统一格式 {"error": {"code": "<error_code>", "message": "<描述>"}}
2. WHILE debug_mode 为 false 时, THE Gateway SHALL 在 5xx 错误中仅返回通用消息，不包含堆栈跟踪
3. WHILE debug_mode 为 true 时, THE Gateway SHALL 在 5xx 错误响应中额外包含 "detail" 字段
4. THE Gateway SHALL 在所有错误响应中包含 X-Request-ID 响应头

### Requirement 12: 缓存回填完整性

**User Story:** As a 系统架构师, I want 所有缓存层级的回填逻辑完整, so that 缓存命中率最大化。

#### Acceptance Criteria

1. WHEN L2 Redis 缓存命中时, THE Cache_Manager SHALL 同步回填 L1 进程缓存
2. WHEN L3 Qdrant 语义缓存命中时, THE Cache_Manager SHALL 同步回填 L1 并异步回填 L2
3. WHEN 全部缓存未命中且 LLM 返回响应后, THE Cache_Manager SHALL 回填 L1 和 L2，并异步计算 embedding 向量回填 L3
4. THE Cache_Manager SHALL 使用 asyncio.create_task 执行 L3 回填操作，不阻塞响应
5. IF L3 回填中 embedding 计算失败, THEN SHALL 记录 WARNING 并跳过，不影响请求响应

### Requirement 13: 熔断器与告警联动

**User Story:** As a 运维工程师, I want 熔断器状态变化触发告警, so that 能及时发现下游 LLM 服务异常。

#### Acceptance Criteria

1. WHEN Circuit_Breaker 从 CLOSED 变为 OPEN 时, THE Metrics_Collector SHALL 设指标为 1 并记录 ERROR 日志
2. WHEN Circuit_Breaker 从 OPEN 变为 HALF_OPEN 时, THE Metrics_Collector SHALL 设指标为 2 并记录 INFO 日志
3. WHEN Circuit_Breaker 恢复为 CLOSED 时, THE Metrics_Collector SHALL 设指标为 0 并记录 INFO 日志
4. THE Gateway SHALL 在 /admin/metrics-json 中包含各 provider 的 circuit_breaker 状态和状态变更时间戳

### Requirement 14: 配置热重载可靠性

**User Story:** As a 运维工程师, I want 配置热重载过程可靠且可观测, so that 配置变更不会导致服务中断。

#### Acceptance Criteria

1. WHEN config.yaml 被修改时, THE Config_Manager SHALL 先验证新配置再交换
2. IF 验证失败, THEN 保持当前配置不变并记录 ERROR 日志
3. WHEN 验证通过时, THE Config_Manager SHALL 原子交换配置
4. WHILE 热重载进行中时, THE Gateway SHALL 确保正在处理的请求使用旧配置完成

### Requirement 15: 控制面板配置编辑

**User Story:** As a 管理员, I want 通过控制面板直接编辑系统配置, so that 不需要 SSH 到服务器修改 YAML 文件。

#### Acceptance Criteria

1. THE Control_Panel SHALL 新增"系统配置"菜单页面
2. THE 配置页面 SHALL 展示当前 config.yaml 的关键段（providers、plugins、infrastructure）为可编辑表单
3. WHEN 用户修改配置并点击"保存"时, THE Control_Panel SHALL 调用 PUT /admin/global-config 写回 config.yaml
4. THE Control_Panel SHALL 在保存前显示变更预览（diff 视图）
5. IF 保存失败, THEN THE Control_Panel SHALL 显示具体错误原因并保留用户的编辑内容

### Requirement 16: 请求日志分页与删除

**User Story:** As a 运维工程师, I want 请求日志支持分页浏览和批量删除, so that 能高效管理大量日志且不影响页面性能。

#### Acceptance Criteria

1. THE Control_Panel 请求日志页面 SHALL 实现服务端分页，每页默认显示 50 条记录
2. THE Control_Panel SHALL 在日志表格底部显示分页控件（上一页、下一页、页码、总条数）
3. THE Control_Panel SHALL 提供"清空日志"按钮，点击后调用 DELETE /admin/logs 清除所有请求日志
4. THE Backend SHALL 实现 DELETE /admin/logs 端点，从 Redis ZSET 中删除所有请求日志记录
5. THE Control_Panel SHALL 在执行清空操作前显示确认对话框 "确定清空所有请求日志？此操作不可撤销"
6. WHEN 清空成功后, THE Control_Panel SHALL 刷新当前页面显示空状态

### Requirement 17: 请求日志 ID 完整显示

**User Story:** As a 开发者, I want 日志中的 request_id 和 trace_id 完整显示, so that 能直接复制完整 ID 用于调试。

#### Acceptance Criteria

1. THE Control_Panel 日志表格 SHALL 完整显示 request_id 和 trace_id（不截断）
2. THE Control_Panel SHALL 对长 ID 使用等宽字体和适当的列宽，必要时允许水平滚动
3. WHEN 用户点击 request_id 或 trace_id 时, THE Control_Panel SHALL 将完整值复制到剪贴板并显示"已复制"提示
4. THE Control_Panel SHALL 使用 CSS word-break 确保 ID 在列宽不足时换行而非溢出隐藏

### Requirement 18: RAG 知识库管理

**User Story:** As a 管理员, I want 通过控制面板添加文档或网页链接到向量数据库, so that 系统能利用自定义知识库增强 AI 回答质量。

#### Acceptance Criteria

1. THE Control_Panel SHALL 新增"知识库"菜单页面，展示已导入的文档列表
2. THE Control_Panel SHALL 支持两种导入方式：上传本地文件（PDF、TXT、Markdown）和输入网页 URL
3. THE Control_Panel SHALL 提供 embedding chunk 配置面板，允许用户设置：
   - 分块策略：按段落（paragraph）、按固定字符数（fixed_size）、按句子（sentence）
   - chunk_size：每块最大字符数（默认 512）
   - chunk_overlap：相邻块重叠字符数（默认 64）
4. THE Backend SHALL 实现 POST /admin/rag/documents 端点，接收文件上传或 URL，按配置的分块策略处理后存入 Qdrant rag_documents 集合
5. THE Backend SHALL 实现 GET /admin/rag/documents 端点，返回已导入文档列表（含文件名、类型、chunk 数、导入时间）
6. THE Backend SHALL 实现 DELETE /admin/rag/documents/{doc_id} 端点，删除指定文档及其在 Qdrant 中的所有向量
7. WHEN 文档处理完成后, THE Backend SHALL 返回处理结果（chunk 数量、总 token 数、耗时）
8. WHEN 导入网页 URL 时, THE Backend SHALL 抓取网页正文内容（去除导航栏、广告等非正文元素）后进行分块和嵌入
