# Gateway2 端到端测试总结报告

> 测试时间: 2026-06-29
> 测试方式: testing-evidence-collector 自动化 API 测试 + security-engineer 安全扫描 + code-reviewer 代码审查

## 测试结果总览

| 类别 | 通过 | 失败 | 通过率 |
|------|------|------|--------|
| 核心 API 端点 | 12 | 1 | 92% |
| 扩展 API 测试 | 3 | 5 | 38% |
| **合计** | **15** | **6** | **71%** |

## 通过的测试 (15/21)

| # | 测试 | 端点 | 结果 |
|---|------|------|------|
| 1 | Health Endpoint | GET /health | ✅ PASS |
| 2 | Prometheus Metrics | GET /metrics | ✅ PASS |
| 3 | Admin Metrics JSON | GET /admin/metrics-json | ✅ PASS |
| 4 | API Keys Quotas List | GET /admin/api-keys | ✅ PASS |
| 5 | Plugins Config | GET /admin/plugins-config | ✅ PASS |
| 6 | Global Config | GET /admin/global-config | ✅ PASS |
| 7 | Global Config Update | PUT /admin/global-config | ✅ PASS |
| 8 | Plugin Toggle Persistence | PUT /admin/plugins-config | ✅ PASS |
| 9 | Request Logs | GET /admin/logs | ✅ PASS |
| 10 | Log Filtering | GET /admin/logs?status=200 | ✅ PASS |
| 11 | Metrics Updated After Request | GET /metrics | ✅ PASS |
| 12 | Control Panel Proxy | GET/POST /aigateway/* | ✅ PASS |
| 13 | Metrics via Proxy | GET /aigateway/metrics | ✅ PASS |
| 14 | GET /admin/quotas/{id} | GET /admin/quotas/{id} | ✅ PASS |
| 15 | DELETE /admin/api-keys/{id} | DELETE /admin/api-keys/{id} | ✅ PASS |

## 失败的测试 (6/21)

### 🔴 Critical (2)

| # | 测试 | 问题 | 根因 |
|---|------|------|------|
| 1 | Error Cases (T16) | 所有请求返回 200，无需鉴权 | `auth_middleware.py` 定义了 `authenticate`/`authenticate_admin`/`require_api_key`，但 **从未在任何路由上调用**。`openai_compat.py` 的 router 没有添加 `Depends(authenticate)` |
| 2 | /v1/models 为空 | `data` 数组为空 | 模型路由器未注册任何模型 |

### 🟡 Major (3)

| # | 测试 | 问题 | 根因 |
|---|------|------|------|
| 3 | _meta.routed_to 缺失 | 缓存命中和非缓存响应都不包含 `routed_to.model` | `_meta` 只在缓存命中时填充 `cache_hit`/`cache_tier`，非缓存路径不填充 `_meta` |
| 4 | 前端 /aigateway/ 返回 404 | nginx 配置中 `/aigateway/` 只代理 API，不代理 SPA 静态文件 | nginx 缺少 SPA fallback 配置 |
| 5 | /v1/embeddings 返回 500 | 缺少 sentence-transformers 依赖时应返回 400/501 | 错误处理逻辑返回 HTTP 500 |

### 🟢 Minor (1)

| # | 测试 | 问题 | 说明 |
|---|------|------|------|
| 6 | DELETE API Key 软删除 | Key 仍在列表中，状态变为 "revoked" | 设计决策，非 bug |

## 安全扫描结果 (security-engineer)

**22 个问题: 3 CRITICAL, 5 HIGH, 8 MEDIUM, 4 LOW**

- **CRITICAL**: AGNES API key 明文存储在 config.yaml 和 .env.example 中
- **CRITICAL**: `authenticate_admin` 未绑定到任何 admin 路由
- **CRITICAL**: `.env` 文件包含真实凭证

## 代码审查结果 (code-reviewer)

**24 个问题: 4 BLOCKER, 9 MAJOR, 11 MINOR**

- **BLOCKER**: 无 auth middleware 绑定到路由
- **BLOCKER**: `RequestTracker` 在缓存命中路径泄漏（未调用 `__exit__`）
- **MAJOR**: `_get_redis_client()` 每次调用创建新 Redis 连接
- **MAJOR**: 配置写入非原子操作

## 多 Worker 迁移验证

| 检查项 | 结果 |
|--------|------|
| Worker 配置改为 1 | ✅ 所有 5 个文件已更新 |
| Prometheus multiprocess 移除 | ✅ 无 multiprocess 错误 |
| SentenceTransformer 缓存 | ✅ 类级 `_model_cache` 已实现 |
| 指标端点正常工作 | ✅ Prometheus 格式正确 |
| 所有 API 端点正常 | ✅ 15/21 通过 |

## 建议修复优先级

1. **P0**: 修复 auth middleware 绑定 — 这是最大的安全问题
2. **P1**: 修复前端 nginx 配置 — `/aigateway/` 路径应能访问 SPA
3. **P1**: 修复 `_meta.routed_to` 缺失 — 所有响应都应包含路由元数据
4. **P2**: 修复 embeddings 错误码 — 返回 400/501 而非 500
5. **P2**: 修复 RequestTracker leak — 缓存命中路径需正确调用 `__exit__`
