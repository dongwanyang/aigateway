# QA 认证测试指南

本项目的认证模型已经分成两条链路：

- 控制台 / 管理接口：用户名密码登录，浏览器或测试客户端使用 HttpOnly Session Cookie。
- OpenAI 兼容程序化接口：使用 `Authorization: Bearer <gateway-api-key>`。

不要再用同一个 API Key 测所有功能，也不要把 AI Gateway 的客户端 API Key 写入 `config.yaml`。

## 1. 认证边界

| 场景 | 正确认证方式 | 典型接口 |
| --- | --- | --- |
| 控制台登录 | 用户名 + 密码 | `POST /auth/session` |
| 控制台状态 | Session Cookie | `GET /auth/session` |
| 控制台退出 | Session Cookie | `DELETE /auth/session` |
| 控制台配置、插件、配额、日志、RAG 管理 | Session Cookie | `/admin/*` |
| 控制台聊天 | Session Cookie，服务端内部使用 console key | `/admin/console/chat/completions` |
| SDK / CLI / 外部程序调用 | Bearer API Key | `/v1/models`, `/v1/chat/completions`, `/v1/embeddings` |

必须保留的安全边界：

- 未登录访问 `/admin/*` 应返回 401/403。
- 只带浏览器 Cookie 调 `/v1/*` 不应被当作客户端 API Key 放行。
- 带有效 Bearer API Key 调 `/v1/*` 应放行。
- Bearer API Key 不应替代控制台登录。

## 2. `config.yaml` 中不再放什么

`config.yaml` 不应保存 AI Gateway 发给客户端使用的 `gw-*` API Key。

客户端 API Key 应由管理员通过 `/admin/api-keys` 创建，落在后端持久化存储中。测试时只能从以下位置取得：

- 测试初始化脚本动态创建。
- `.env.test` / `.env.test.local`。
- CI Secret。
- Postman / Apifox environment。

`config.yaml` 或 `.env` 仍可以通过环境变量引用上游模型提供商密钥，例如 OpenAI、DeepSeek、Anthropic 等 provider key。它们是服务端调用上游模型用的密钥，不是客户端调用 AI Gateway 的认证凭证。

## 3. curl QA 流程

### 3.1 登录控制台并保存 Cookie

```bash
BASE_URL=${BASE_URL:-http://localhost:8000}
QA_ADMIN_USERNAME=${QA_ADMIN_USERNAME:-admin}
: "${QA_ADMIN_PASSWORD:?missing QA_ADMIN_PASSWORD}"

curl -sS -i -c /tmp/aigateway-qa-cookies.txt \
  -H "Content-Type: application/json" \
  -X POST "$BASE_URL/auth/session" \
  -d "{\"username\":\"$QA_ADMIN_USERNAME\",\"password\":\"$QA_ADMIN_PASSWORD\"}"
```

后续控制台接口必须带 Cookie：

```bash
curl -sS -b /tmp/aigateway-qa-cookies.txt \
  "$BASE_URL/admin/config"

curl -sS -b /tmp/aigateway-qa-cookies.txt \
  "$BASE_URL/admin/plugins-config"
```

### 3.2 创建 QA 专用 API Key

```bash
QA_KEY_RESPONSE=$(curl -sS -b /tmp/aigateway-qa-cookies.txt \
  -H "Content-Type: application/json" \
  -X POST "$BASE_URL/admin/api-keys" \
  -d '{
    "user_id": "qa-user",
    "cache_scope": "private",
    "daily_tokens": 100000,
    "monthly_cost": 10,
    "rate_limit_rpm": 600,
    "rate_limit_tpm": 200000
  }')

echo "$QA_KEY_RESPONSE"
```

接口返回的明文 key 只会出现一次。QA 脚本应立即取出 `data.key`，保存到测试进程变量或 Secret，不要写入仓库文件。

如果本地有 `jq`：

```bash
export QA_API_KEY=$(echo "$QA_KEY_RESPONSE" | jq -r '.data.key')
```

### 3.3 用 API Key 测 `/v1/*`

```bash
curl -sS "$BASE_URL/v1/models" \
  -H "Authorization: Bearer $QA_API_KEY"

curl -sS "$BASE_URL/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $QA_API_KEY" \
  -d '{
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": "hello"}]
  }'
```

## 4. pytest 推荐 fixture

```python
import os

import pytest
import requests

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")


@pytest.fixture
def admin_session() -> requests.Session:
    session = requests.Session()
    response = session.post(
        f"{BASE_URL}/auth/session",
        json={
            "username": os.getenv("QA_ADMIN_USERNAME", "admin"),
            "password": os.environ["QA_ADMIN_PASSWORD"],
        },
        timeout=10,
    )
    response.raise_for_status()
    return session


@pytest.fixture
def qa_api_key(admin_session: requests.Session) -> str:
    response = admin_session.post(
        f"{BASE_URL}/admin/api-keys",
        json={
            "user_id": "qa-user",
            "cache_scope": "private",
            "daily_tokens": 100000,
            "monthly_cost": 10,
            "rate_limit_rpm": 600,
            "rate_limit_tpm": 200000,
        },
        timeout=10,
    )
    response.raise_for_status()
    return response.json()["data"]["key"]
```

示例测试：

```python
def test_admin_config_requires_login(admin_session: requests.Session) -> None:
    response = admin_session.get(f"{BASE_URL}/admin/config", timeout=10)
    assert response.status_code == 200


def test_v1_models_accepts_gateway_api_key(qa_api_key: str) -> None:
    response = requests.get(
        f"{BASE_URL}/v1/models",
        headers={"Authorization": f"Bearer {qa_api_key}"},
        timeout=10,
    )
    assert response.status_code == 200


def test_v1_rejects_cookie_only(admin_session: requests.Session) -> None:
    response = admin_session.get(f"{BASE_URL}/v1/models", timeout=10)
    assert response.status_code in {401, 403}
```

## 5. 前端 / Vitest / Playwright 规则

前端控制台代码不得再读取或写入 `localStorage.aigateway_api_key`。控制台请求应使用：

```ts
fetch(path, { credentials: 'include' })
```

前端集成测试 mock 时也要按新边界写：

- `/auth/session` 返回登录态。
- `/admin/*` mock Session Cookie 已登录后的行为。
- `/admin/console/chat/completions` 是控制台聊天端点。
- `/v1/*` 的程序化调用测试应单独 mock Bearer API Key，不要混进控制台登录测试。

## 6. Postman / Apifox 规则

建议使用以下环境变量：

```text
BASE_URL=http://localhost:8000
QA_ADMIN_USERNAME=admin
QA_ADMIN_PASSWORD=<secret>
QA_API_KEY=<created-by-admin-api-keys>
```

测试集合拆成两组：

1. Admin / Console：先 `POST /auth/session`，保存 Cookie，后续请求自动带 Cookie。
2. OpenAI-compatible API：请求头固定为 `Authorization: Bearer {{QA_API_KEY}}`。

不要让 Postman 把 `QA_API_KEY` 写入 `config.yaml`、README、示例响应或测试快照。

## 7. 给其他模型或代理的操作准则

处理认证、测试或 QA 相关任务时，先遵守本文件：

- 不要恢复旧的前端 API Key 登录模式。
- 不要在 `config.yaml`、`config.yaml.template`、README 示例中硬编码真实 `gw-*` key。
- 不要把 `/admin/*` 改回 Bearer API Key 鉴权。
- 不要把 `/v1/*` 改成仅 Cookie 鉴权。
- 写测试时必须覆盖 Cookie 与 Bearer 的边界，而不是只测成功路径。
