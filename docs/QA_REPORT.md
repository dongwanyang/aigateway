# QA Evidence-Based Report

## Phase 5 Backend Implementation Verification -- FAIL -- 2026-06-27T10:30:00Z

### 现实核查结果
**Commands Executed**:
1. `find /home/ubuntu/gateway2 -type f -name "*.py" | sort` -- 列出所有 Python 源文件
2. `grep -rn "@app\.\(post\|get\|delete\|put\|patch\)" /home/ubuntu/gateway2/aigateway-api/src/ --include="*.py"` -- 查找 FastAPI 路由定义
3. `grep -rn "router\|APIRouter\|@api_router\|@app\." /home/ubuntu/gateway2/aigateway-api/src/ --include="*.py"` -- 查找路由器挂载
4. `grep -rn "aigateway:key:\|aigateway:key_lookup:\|aigateway:cache:v1:\|aigateway:quota:\|aigateway:ratelimit:" /home/ubuntu/gateway2/ --include="*.py"` -- Redis key 前缀核对
5. `grep -rn "class PipelineContext\|request\|response\|should_stop\|should_stream\|trace_id\|request_id\|user_id\|extra" /home/ubuntu/gateway2/aigateway-core/src/aigateway_core/context.py` -- PipelineContext 字段检查
6. `grep -rn "daily_tokens\|monthly_cost\|rate_limit_rpm\|rate_limit_tpm\|rpm\|tpm\|quota" /home/ubuntu/gateway2/aigateway-core/src/aigateway_core/security.py` -- 配额检查
7. `grep -rn "error.*code\|error.*message\|unauthorized\|forbidden\|quota_exceeded\|rate_limit\|circuit_breaker\|upstream_timeout\|internal_error\|validation_error\|pii_rejected\|invalid_model\|not_found\|service_unavailable" /home/ubuntu/gateway2/aigateway-api/src/ --include="*.py"` -- 错误码检查
8. `grep -rn "chat.completion\|chat.completion.chunk\|choices\|usage\|_meta\|tool_calls\|delta\|finish_reason" /home/ubuntu/gateway2/aigateway-api/src/ --include="*.py"` -- 响应结构检查

**Evidence Files**:
- `/home/ubuntu/gateway2/docs/qa-evidence-20260627/routes.txt`
- `/home/ubuntu/gateway2/docs/qa-evidence-20260627/error-format.txt`
- `/home/ubuntu/gateway2/docs/qa-evidence-20260627/response-structure.txt`
- `/home/ubuntu/gateway2/docs/qa-evidence-20260627/redis-keys.txt`
- `/home/ubuntu/gateway2/docs/qa-evidence-20260627/quota-check.txt`

**Specification Quote**: "所有业务接口统一使用 /v1/ 前缀（OpenAI 兼容）" / "所有管理接口统一使用 /admin/ 前缀" / "统一错误格式: { "error": { "code": "error_code", "message": "人类可读描述" } }"

### 证据分析

**Module Existence Check**:
- aigateway-api/src/aigateway_api/ 目录仅有 2 个文件: `__init__.py` 和 `main.py`
- `main.py` 在第 186 行 `from . import admin_routes, openai_compat, routes` 和 189/192/195 行引用了三个不存在的模块
- **`openai_compat.py` 缺失** -- 这是实现 /v1/chat/completions, /v1/models, /v1/embeddings 的核心文件
- **`admin_routes.py` 缺失** -- 这是实现 /admin/api-keys, /admin/api-keys/{key_id}, /admin/quotas/{key_id} 的核心文件
- **`auth_middleware.py` 缺失** -- 这是实现 API Key 认证中间件的文件
- **`streaming.py` 缺失** -- 这是实现 SSE 流式响应的文件
- **`routes.py` 缺失** -- 这是实现 /metrics, /health 的文件

**Core Library Coverage**:
- aigateway-core 已实现: security.py (KeyStore), caching.py (CacheManager), context.py (PipelineContext), pipeline.py (PipelineEngine), metrics.py (MetricsCollector), litellm_bridge.py (LiteLLMBridge), redis_client.py, circuit_breaker.py, plugin_registry.py, config.py, tracing.py, logger.py, qdrant_client.py
- Core 层实现了业务逻辑骨架，但 **API 层（路由/响应/错误处理）完全缺失**

### 找到的问题

1. **CRITICAL: API 路由模块全部缺失**
   **Evidence**: `aigateway-api/src/aigateway_api/` 目录仅含 `__init__.py` (9 字节) 和 `main.py`。`main.py` 第 186 行 `from . import admin_routes, openai_compat, routes` 会在任何导入时抛出 `ModuleNotFoundError`。
   **Impact**: 整个 API 服务无法启动，所有 9 个端点 (/v1/chat/completions, /v1/models, /v1/embeddings, /admin/api-keys, /admin/api-keys, /admin/api-keys/{key_id}, /admin/quotas/{key_id}, /metrics, /health) 均未实现。
   **Priority**: Critical

2. **CRITICAL: 错误响应格式未映射**
   **Evidence**: `security.py` 定义了 `GatewayError`, `AuthError`, `QuotaExceededError` 三个异常类，但没有任何 FastAPI 异常处理器（`@app.exception_handler`）将这些异常映射为 HTTP 状态码和统一 JSON 错误格式。`PipelineEngine._build_error_response` (pipeline.py:288) 仅返回 `internal_error` 一种错误码，缺少 spec 要求的 14+ 种错误码映射。
   **Impact**: 即使路由存在，所有认证失败、配额超限、熔断器打开等场景也无法返回正确的 HTTP 状态码和错误码。
   **Priority**: Critical

3. **CRITICAL: 配额检查返回中文消息，不符合 spec**
   **Evidence**: `security.py` check_quota 方法 (lines 356, 372, 378, 384) 返回的失败消息为中文（"RPM 限额已超限"、"TPM 限额已超限"、"日 token 配额已耗尽"、"月成本配额已耗尽"）。API_CONTRACT.md 明确要求英文消息（"Rate limit exceeded: {current}/{limit} requests per minute" 等）。
   **Impact**: 国际化错误消息不合规。
   **Priority**: Medium

4. **CRITICAL: 缺少 Retry-After 头实现**
   **Evidence**: API_CONTRACT.md 429 错误码表明确列出了 `retry_after_header_present` 错误码，要求响应头携带 `Retry-After: {seconds}`。`security.py` check_quota 虽然计算了 `retry_after` 变量 (lines 355, 371)，但没有将其传递给调用方，也没有任何代码处理 HTTP 响应头的设置。
   **Impact**: 所有 429 响应缺少必需的 Retry-After 头。
   **Priority**: Medium

5. **CRITICAL: CircuitBreakerOpenError 继承层次错误**
   **Evidence**: `circuit_breaker.py` line 52: `class CircuitBreakerOpenError(Exception)` -- 直接继承 `Exception`。TECH_SPEC.md 明确要求 `GatewayError -> CircuitBreakerOpenError`。
   **Impact**: 异常层次不符合 spec，无法被统一的 GatewayError 处理器捕获。
   **Priority**: Medium

6. **BUG: redis_client.py delete_api_key 中 key_lookup 删除逻辑未实现**
   **Evidence**: `redis_client.py` line 199: `lookup_key = f"aigateway:key_lookup"` (缺少 `{key_prefix}`)，注释写着 "# 实际使用前需确定 key_prefix"。这是一个 TODO 未完成标记。
   **Impact**: 删除 API Key 时不会删除反向查找记录，导致 key_prefix 残留。
   **Priority**: Medium

7. **MISSING: LiteLLMBridge 错误码与 spec 不一致**
   **Evidence**: `litellm_bridge.py` line 288: 返回 `"code": "upstream_error"`。API_CONTRACT.md 要求 504 使用 `"code": "upstream_timeout"`。
   **Impact**: 错误码与 spec 不匹配。
   **Priority**: Medium

8. **MISSING: PipelineContext._meta 字段与 spec 不完全对齐**
   **Evidence**: `context.py` `add_plugin_trace` 方法正确实现了 `plugin_trace` 字段。但 `PipelineEngine._build_response` (pipeline.py:279-285) 返回的 `_meta` 包含 `trace_id`, `request_id`, `user_id`, `should_stream` -- 这些不在 API_CONTRACT.md 的 `_meta` 定义中。Spec 的 `_meta` 期望 `cache_hit`, `cache_tier`, `plugin_trace`, `routed_to`。
   **Impact**: 响应结构中 `_meta` 字段多余且缺失关键字段。
   **Priority**: Low

9. **MISSING: 流式响应实现完全不存在**
   **Evidence**: 项目中不存在 `streaming.py` 文件，`main.py` 也不包含任何 `StreamingResponse` 或 SSE 相关代码。API_CONTRACT.md 详细定义了 `chat.completion.chunk` 的 SSE 格式和缓存命中流式响应特殊行为（F15）。
   **Impact**: 流式响应功能完全缺失。
   **Priority**: Critical

10. **MISSING: 认证中间件不存在**
    **Evidence**: 项目中不存在 `auth_middleware.py`。TECH_SPEC.md 目录结构明确列出了 `auth_middleware.py -- API Key 校验中间件`。API_CONTRACT.md 要求所有 `/v1/*` 和 `/admin/*` 接口必须携带 API Key。
    **Impact**: 所有业务接口无认证保护。
    **Priority**: Critical

### 结论：FAIL

**实现级别**: Skeleton Only (核心库骨架已搭建，API 路由层完全缺失)

**具体问题汇总**:
| # | 问题 | 优先级 | 证据文件 |
|---|------|--------|----------|
| 1 | API 路由模块 (openai_compat.py, admin_routes.py, routes.py) 全部缺失 | Critical | routes.txt |
| 2 | 错误处理中间件缺失，异常未映射为 HTTP 响应 | Critical | error-format.txt |
| 3 | 配额检查返回中文消息，不符合 spec 英文要求 | Medium | quota-check.txt |
| 4 | Retry-After 头未实现 | Medium | quota-check.txt |
| 5 | CircuitBreakerOpenError 继承层次错误 | Medium | error-format.txt |
| 6 | delete_api_key 中 key_lookup 删除逻辑未完成 | Medium | redis-keys.txt |
| 7 | LiteLLMBridge 错误码 upstream_error vs upstream_timeout | Medium | error-format.txt |
| 8 | PipelineContext _meta 字段与 spec 不对齐 | Low | response-structure.txt |
| 9 | 流式响应 (streaming.py) 完全缺失 | Critical | routes.txt |
| 10 | 认证中间件 (auth_middleware.py) 完全缺失 | Critical | routes.txt |

**真实质量评估**:
- **Rating**: D- (核心骨架存在，API 层完全未实现)
- **Implementation Level**: Basic (仅核心库骨架)
- **Production Readiness**: FAILED

**必需修复**:
1. 创建 `openai_compat.py` -- 实现 /v1/chat/completions, /v1/models, /v1/embeddings 路由
2. 创建 `admin_routes.py` -- 实现 /admin/api-keys CRUD, /admin/quotas/{key_id} 路由
3. 创建 `routes.py` -- 实现 /metrics, /health 路由
4. 创建 `streaming.py` -- 实现 SSE 流式响应
5. 创建 `auth_middleware.py` -- 实现 API Key 校验中间件
6. 实现 FastAPI 异常处理器，映射 GatewayError 层次到 HTTP 状态码和统一错误格式
7. 修正所有错误消息为英文
8. 实现 Retry-After 头
9. 修正 CircuitBreakerOpenError 继承层次
10. 修复 redis_client.py delete_api_key 中的 key_lookup bug
11. 修正 LiteLLMBridge 错误码为 upstream_timeout

---
**QA Agent**: EvidenceQA
**Evidence Date**: 2026-06-27
**Evidence Path**: /home/ubuntu/gateway2/docs/qa-evidence-20260627/
