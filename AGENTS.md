# AGENTS.md

本文件给后续 AI 编程代理、代码审查模型和自动化修复工具使用。执行代码修改前，先阅读本文件和相关专项文档。

## 项目测试优先级

认证、控制台、API Key、QA 测试相关修改必须先阅读：

- `docs/QA_AUTH_TESTING.md`

当前认证边界是硬约束：

- 控制台与 `/admin/*` 使用用户名密码登录后的 HttpOnly Session Cookie。
- OpenAI 兼容 `/v1/*` 使用 `Authorization: Bearer <gateway-api-key>`。
- AI Gateway 客户端 API Key 不写入 `config.yaml`。
- 不要恢复旧的前端 `localStorage.aigateway_api_key` 登录模式。
- 不要把 `/admin/*` 改回 Bearer API Key 鉴权。
- 不要把 `/v1/*` 改成仅 Cookie 鉴权。

## 认证测试最低要求

涉及认证或 QA 流程的改动，至少要覆盖以下边界：

1. 未登录访问 `/admin/*` 应失败。
2. 用户名密码登录后，带 Cookie 访问 `/admin/*` 应成功。
3. 只带 Cookie 访问 `/v1/*` 不应被当作 API Key 放行。
4. 带有效 Bearer API Key 访问 `/v1/*` 应成功。
5. 创建、轮换、吊销 API Key 后，配额与调用链路仍按 key 维度工作。

## 文档维护规则

当认证模型、QA 脚本、测试 fixture、Postman/Apifox 集合或 CI Secret 名称变化时，必须同步更新 `docs/QA_AUTH_TESTING.md`。

示例中的密钥只能使用占位符，不能提交真实 `gw-*` key、provider key、Cookie 或 session token。
