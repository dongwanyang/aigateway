AGENTS.md

Repository-wide guidance for coding agents working on AI Gateway.

Scope and precedence

This file applies to the entire repository. A nested AGENTS.md orAGENTS.override.md may add stricter rules for its subtree.

Follow the user's current request first, then the closest applicable agentguidance. Do not silently reinterpret product requirements.

Read only the relevant parts of AGENTS.md，README.md, CLAUDE.md, SECURITY.md,docs/RUNTIME_MAP.md, and docs/DB_SCHEMA.md. Treat executable code andtests as evidence of current behavior; treat the product contracts below asthe intended behavior when older comments or documents disagree.

Never commit, push, merge, publish packages, rotate credentials, or modifyexternal systems unless the user explicitly requests that action.

Preserve unrelated working-tree changes. Keep each patch narrowly scoped.

Project map

aigateway-api/: FastAPI protocol and HTTP boundary.

aigateway-core/: dispatcher, pipelines, plugins, provider bridge, caching,auth persistence, and shared runtime logic.

aigateway-cli/: command-line client.

control-panel/: React 19, TypeScript, Vite, Vitest, and React Query.

tests/unit/: deterministic backend tests grouped by subsystem.

tests/e2e/ and tests/ui/: tests that require live services.

config.yaml: active runtime configuration.

config.yaml.template: configuration schema and documented defaults.

docker-compose.yml: local single-node stack.

Use Python 3.12. Install editable Python packages in this order:

python3 -m pip install -e aigateway-core
python3 -m pip install -e "aigateway-api[dev]"
python3 -m pip install -e aigateway-cli

Non-negotiable product contracts

Deployment scope

The current product is a single-organization/internal AI Gateway. Do not addtenant or organization isolation, tenant quotas, or tenant-scoped cachelayers without an explicit product decision.

Keep user, API-key, group, scope, quota, and cost controls. Lightweightextension points are acceptable; speculative multi-tenant complexity is not.

Production currently runs one Uvicorn worker. Do not rely on that fact fortransaction safety, idempotency, cleanup, or correctness.

Storage ownership

SQLite is the production source of truth for API keys, groups, quota state,usage, and cost. Do not move quota persistence to Redis.

Quota mutations must be atomic and race-safe. Keep state used for enforcementseparate from immutable or append-oriented usage history where applicable.

Redis means Redis Stack, not feature-equivalent plain Redis. It owns transientor distributed concerns such as L2 cache/RediSearch BM25, rate limiting,short-lived session or draft state, and Pub/Sub.

Qdrant owns vector/semantic retrieval, long-lived knowledge-base retrieval,and Code RAG collections.

Unit tests may use fakes, but integration tests for RediSearch behavior mustexercise Redis Stack. Do not claim BM25 coverage from a plain Redis mock.

Authentication boundary

/v1/* is machine-facing and must authenticate with an API key only. Browsersession cookies must never authorize /v1/*.

Browser session cookies are limited to /auth/* and /admin/*.

A session with requires_password_change may only change its password or logout. It must not access other admin routes.

Control-panel chat must use an authenticated /admin/console/* server-sideadapter. The backend binds AI_GATEWAY_CONSOLE_CHAT_API_KEY orADMIN_API_KEY; do not expose that key to browser JavaScript.

Keep scope checks explicit at route boundaries. Authentication success is notauthorization.

Never log or return raw API keys, provider secrets, session tokens, passwordmaterial, connection strings, or secret-bearing exception messages.

Security enforcement (auth, scope, quota, path validation) fails closed.Optional optimization plugins may fail open only when that behavior isdeliberate, bounded, logged, and covered by tests.

Runtime lifecycle

Construct the app through create_app and initialize shared runtime resourcesin FastAPI lifespan. Store them on app.state and access them through theexisting state/dependency helpers.

Do not create network clients, event-loop-bound objects, schedulers, orbackground tasks as import-time side effects.

Every resource opened during startup needs deterministic shutdown. Hold strongreferences to background tasks, propagate cancellation, and await or cancelthem during shutdown.

Never block the event loop with model loading, subprocess waits, filesystemscans, SQLite-heavy work, or synchronous provider calls. Use an async API or abounded worker thread/process as appropriate.

Apply the request deadline, plugin timeout, retry budget, and providercooldown consistently. Avoid nested retries that multiply latency.

Request pipeline and protocol

Keep the API package thin. Reusable orchestration and business logic belong inaigateway-core; HTTP parsing and response adaptation belong inaigateway-api.

Preserve the dispatcher flow and the distinction between understanding,image-generation, and video-generation pipelines. Do not reintroduce theremoved classic model_router plugin; runtime resolution belongs in theLiteLLM bridge/model-resolution layer.

Preserve OpenAI-compatible response and error shapes for /v1/*.

Streaming and non-streaming paths must have parity for authentication, quotareservation/commit/release, actual routed-model logging, usage/costaccounting, cache writes, and error mapping.

SSE code must handle client disconnect and cancellation promptly, avoidorphaned provider work, and emit at most one terminal marker. Never report asuccessful terminal event after an upstream failure.

Cache keys must include every input that can materially change a response.Keep PII-derived requests private and do not cache secrets or unsafe toolresults.

Configuration and dependencies

Python dependencies belong only in the relevant pyproject.toml; do notduplicate package lists in Dockerfiles.

When adding or changing configuration, update code defaults,config.yaml.template, relevant docs, and tests in the same change. Preserveprecedence: process environment, .env, YAML, then code defaults.

Keep provider credentials as environment-variable references. Examples mustuse obvious placeholders, never plausible live secrets.

All config.yaml writes must remain atomic (tempfile plus os.replace orthe existing helper). Do not restore truncate-then-write behavior.

Pin container image versions intentionally. Do not replace Redis Stack with aplain Redis image.

Implementation workflow

Inspect the current implementation, callers, tests, and configuration beforeediting. For bug fixes, first establish a reproducible failure or a concretecode path.

State assumptions when evidence is incomplete. Separate verified facts,inferences, and recommendations; do not present a hypothetical vulnerabilityas confirmed.

Add or update a regression test that fails for the old behavior. Preferbehavior-focused assertions over implementation details.

Make the smallest coherent change. Avoid unrelated renames, formatting,dependency upgrades, or architectural rewrites.

Run the narrowest relevant tests during development, then the requiredbroader gates below.

Review the diff for auth bypass, secret exposure, race conditions, lostcancellation, unbounded retries, blocking async work, schema drift, andstreaming/non-streaming divergence.

Report changed files, commands run, results, tests not run and why, and anyremaining risk. Never describe a skipped or unexecuted check as passing.

Coding conventions

Python

Use typed Python 3.12. Add types to new or changed public interfaces.

Prefer explicit dependencies and small pure helpers. Avoid new module-levelmutable state; use app state, dependency injection, or an owned service.

Catch the narrowest exception possible. Preserve cancellation exceptions.Broad catches at an isolation boundary must log structured context withoutsecrets and must not convert security failures into success.

Use parameterized SQL only. Keep transactions short and make concurrent quotaor ledger behavior explicit.

Use monotonic time for durations and deadlines; use timezone-aware UTC forpersisted timestamps.

Include trace_id/request_id in operational logs where available. Do not useprint in runtime code.

TypeScript and React

Keep TypeScript strict; do not bypass errors with any, @ts-ignore, orunchecked type assertions without a documented boundary.

Centralize HTTP behavior in control-panel/src/api/client.ts. Do not duplicateauth, base-URL, error, or SSE parsing logic in page components.

Keep server state in React Query and small client/UI state in the existingstore/context patterns. Clean up timers, streams, subscriptions, and abortcontrollers on unmount.

Test user-visible behavior and API contracts with Testing Library/Vitest.Cover loading, empty, error, cancellation, and retry states when relevant.

Test policy

Tests must be deterministic and isolated. Unit tests must not require liveprovider APIs, Redis, Qdrant, wall-clock sleeps, or production credentials.

Use temporary SQLite databases per test or fixture. Add concurrent tests forquota reservation/commit/release and migration changes.

Do not add skip, skipif, or xfail; tests/conftest.py intentionallyrejects them. Missing prerequisites for an explicitly requested integrationsuite are failures, not silent passes.

Mock at system boundaries, not the function under test. Assert durablebehavior: status, response shape, persisted state, emitted events, and cleanup.

Every auth change needs positive and negative cases: missing, invalid,revoked, expired, insufficient scope, wrong credential type, and forcedpassword-change restrictions as applicable.

Every streaming change needs success, upstream error, disconnect/cancel,quota/accounting, and single-terminal-event coverage.

Backend gates

During development, run the changed test module:

python3 -m pytest path/to/test_file.py -q

Before handing off any backend behavior change:

python3 -m pytest tests/unit/ -q
python3 -m ruff check aigateway-core/src aigateway-api/src aigateway-cli/src tests

For auth, session, scope, quota, or admin-boundary changes, also run:

python3 -m pytest \
  tests/unit/auth/ \
  tests/unit/api/ \
  tests/unit/integration/test_p1_security_fixes.py \
  -q

For coverage-sensitive or broad core/API changes:

python3 -m pytest tests/unit/ \
  --cov=aigateway_core \
  --cov=aigateway_api \
  --cov-report=term-missing \
  --cov-report=xml \
  --cov-fail-under=50

Frontend gates

For any control-panel behavior or API-contract change:

cd control-panel
npm test
npm run build

For broad frontend changes:

cd control-panel
npm run test:coverage

The configured frontend line-coverage threshold is 70%.

Integration, container, and benchmark gates

Run python3 -m pytest tests/e2e/ -q only with an intentionally startedgateway and the required test credentials/services.

Run python3 -m pytest tests/ui/ -q when a user journey spans the browser andbackend.

For Compose changes, run docker compose config.

For backend Python, dependency, Dockerfile, or baked configuration changes,rebuild the affected image, check GET /health, and inspect recent gatewaylogs for errors.

Run the benchmark suite when changing routing, caching, compression, tokenaccounting, provider fallback, or cost behavior. Do not require live providersecrets for unrelated unit-test changes.

Documentation and review

Update README.md for user-facing setup or behavior, config.yaml.templatefor configuration schema, and architecture/schema docs when their contractschange.

Update CLAUDE.md only for durable architecture or workflow facts, and keepit concise. Do not add task history or duplicate this file.

Code review findings must identify the concrete file and code path, explainimpact, and name a verification test. Prioritize correctness, security,concurrency, resource lifecycle, and protocol compatibility over style.

Keep this file limited to durable, recurring guidance. When the user correctsa recurring project assumption, update the closest applicable AGENTS.md.
---

# 认证与 QA 测试边界（来自 origin/fix/control-panel-session-auth）

本节给后续 AI 编程代理、代码审查模型和自动化修复工具使用。执行代码修改前，先阅读本文件和相关专项文档。

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
