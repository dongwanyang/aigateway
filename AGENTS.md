# AGENTS.md

Repository-wide guidance for coding agents working on AI Gateway.

## Scope and precedence

This file applies to the entire repository. A nested `AGENTS.md` or `AGENTS.override.md` may add stricter rules for its subtree.

Follow the user's current request first, then the closest applicable agent guidance. Do not silently reinterpret product requirements.

Read only the relevant parts of `AGENTS.md`, `README.md`, `CLAUDE.md`, `SECURITY.md`, `docs/TESTING.md`, `docs/QA_AUTH_TESTING.md`, `docs/RUNTIME_MAP.md`, and `docs/DB_SCHEMA.md`. Treat executable code and tests as evidence of current behavior; treat the product contracts below as the intended behavior when older comments or documents disagree.

Never commit, push, merge, publish packages, rotate credentials, or modify external systems unless the user explicitly requests that action.

Preserve unrelated working-tree changes. Keep each patch narrowly scoped.

## Project map

- `aigateway-api/`: FastAPI protocol and HTTP boundary.
- `aigateway-core/`: dispatcher, pipelines, plugins, provider bridge, caching, auth persistence, and shared runtime logic.
- `aigateway-cli/`: command-line client.
- `control-panel/`: React 19, TypeScript, Vite, Vitest, and React Query.
- `tests/unit/`: deterministic backend tests grouped by subsystem.
- `tests/e2e/` and `tests/ui/`: tests that require live services.
- `config.yaml`: active runtime configuration.
- `config.yaml.template`: configuration schema and documented defaults.
- `docker-compose.yml`: local single-node stack.

Use Python 3.12. Install editable Python packages in this order:

```bash
python3 -m pip install -e aigateway-core
python3 -m pip install -e "aigateway-api[dev]"
python3 -m pip install -e aigateway-cli
```

## Non-negotiable product contracts

### Deployment scope

The current product is a single-organization/internal AI Gateway. Do not add tenant or organization isolation, tenant quotas, or tenant-scoped cache layers without an explicit product decision.

Keep user, API-key, group, scope, quota, and cost controls. Lightweight extension points are acceptable; speculative multi-tenant complexity is not.

Production currently runs one Uvicorn worker. Do not rely on that fact for transaction safety, idempotency, cleanup, or correctness.

### Storage ownership

SQLite is the production source of truth for API keys, groups, quota state, usage, and cost. Do not move quota persistence to Redis.

Quota mutations must be atomic and race-safe. Keep state used for enforcement separate from immutable or append-oriented usage history where applicable.

Redis means Redis Stack, not feature-equivalent plain Redis. It owns transient or distributed concerns such as L2 cache/RediSearch BM25, rate limiting, short-lived session or draft state, and Pub/Sub.

Qdrant owns vector/semantic retrieval, long-lived knowledge-base retrieval, and Code RAG collections.

Unit tests may use fakes, but integration tests for RediSearch behavior must exercise Redis Stack. Do not claim BM25 coverage from a plain Redis mock.

### Authentication boundary

`/v1/*` is machine-facing and must authenticate with an API key only. Browser session cookies must never authorize `/v1/*`.

Browser session cookies are limited to `/auth/*` and `/admin/*`.

A session with `requires_password_change` may only change its password or logout. It must not access other admin routes.

Control-panel chat must use an authenticated `/admin/console/*` server-side adapter. The backend binds `AI_GATEWAY_CONSOLE_CHAT_API_KEY` or `ADMIN_API_KEY`; do not expose that key to browser JavaScript.

Keep scope checks explicit at route boundaries. Authentication success is not authorization.

Never log or return raw API keys, provider secrets, session tokens, password material, connection strings, or secret-bearing exception messages.

Security enforcement (auth, scope, quota, path validation) fails closed. Optional optimization plugins may fail open only when that behavior is deliberate, bounded, logged, and covered by tests.

### Runtime lifecycle

Construct the app through `create_app` and initialize shared runtime resources in FastAPI lifespan. Store them on `app.state` and access them through the existing state/dependency helpers.

Do not create network clients, event-loop-bound objects, schedulers, or background tasks as import-time side effects.

Every resource opened during startup needs deterministic shutdown. Hold strong references to background tasks, propagate cancellation, and await or cancel them during shutdown.

Never block the event loop with model loading, subprocess waits, filesystem scans, SQLite-heavy work, or synchronous provider calls. Use an async API or a bounded worker thread/process as appropriate.

Apply the request deadline, plugin timeout, retry budget, and provider cooldown consistently. Avoid nested retries that multiply latency.

### Request pipeline and protocol

Keep the API package thin. Reusable orchestration and business logic belong in `aigateway-core`; HTTP parsing and response adaptation belong in `aigateway-api`.

Preserve the dispatcher flow and the distinction between understanding, image-generation, and video-generation pipelines. Do not reintroduce the removed classic `model_router` plugin; runtime resolution belongs in the LiteLLM bridge/model-resolution layer.

Preserve OpenAI-compatible response and error shapes for `/v1/*`.

Streaming and non-streaming paths must have parity for authentication, quota reservation/commit/release, actual routed-model logging, usage/cost accounting, cache writes, and error mapping.

SSE code must handle client disconnect and cancellation promptly, avoid orphaned provider work, and emit at most one terminal marker. Never report a successful terminal event after an upstream failure.

Cache keys must include every input that can materially change a response. Keep PII-derived requests private and do not cache secrets or unsafe tool results.

## Configuration and dependencies

Python dependencies belong only in the relevant `pyproject.toml`; do not duplicate package lists in Dockerfiles.

When adding or changing configuration, update code defaults, `config.yaml.template`, relevant docs, and tests in the same change. Preserve precedence: process environment, `.env`, YAML, then code defaults.

Keep provider credentials as environment-variable references. Examples must use obvious placeholders, never plausible live secrets.

All `config.yaml` writes must remain atomic: tempfile plus `os.replace` or the existing helper. Do not restore truncate-then-write behavior.

Pin container image versions intentionally. Do not replace Redis Stack with a plain Redis image.

## Implementation workflow

Inspect the current implementation, callers, tests, and configuration before editing. For bug fixes, first establish a reproducible failure or a concrete code path.

State assumptions when evidence is incomplete. Separate verified facts, inferences, and recommendations; do not present a hypothetical vulnerability as confirmed.

Add or update a regression test that fails for the old behavior. Prefer behavior-focused assertions over implementation details.

Make the smallest coherent change. Avoid unrelated renames, formatting, dependency upgrades, or architectural rewrites.

Run the narrowest relevant tests during development, then the required broader gates below.

Review the diff for auth bypass, secret exposure, race conditions, lost cancellation, unbounded retries, blocking async work, schema drift, and streaming/non-streaming divergence.

Report changed files, commands run, results, tests not run and why, and any remaining risk. Never describe a skipped or unexecuted check as passing.

## Coding conventions

### Python

Use typed Python 3.12. Add types to new or changed public interfaces.

Prefer explicit dependencies and small pure helpers. Avoid new module-level mutable state; use app state, dependency injection, or an owned service.

Catch the narrowest exception possible. Preserve cancellation exceptions. Broad catches at an isolation boundary must log structured context without secrets and must not convert security failures into success.

Use parameterized SQL only. Keep transactions short and make concurrent quota or ledger behavior explicit.

Use monotonic time for durations and deadlines; use timezone-aware UTC for persisted timestamps.

Include `trace_id`/`request_id` in operational logs where available. Do not use `print` in runtime code.

### TypeScript and React

Keep TypeScript strict; do not bypass errors with `any`, `@ts-ignore`, or unchecked type assertions without a documented boundary.

Centralize HTTP behavior in `control-panel/src/api/client.ts`. Do not duplicate auth, base-URL, error, or SSE parsing logic in page components.

Keep server state in React Query and small client/UI state in the existing store/context patterns. Clean up timers, streams, subscriptions, and abort controllers on unmount.

Test user-visible behavior and API contracts with Testing Library/Vitest. Cover loading, empty, error, cancellation, and retry states when relevant.

## Test policy

`docs/TESTING.md` is the general testing source of truth. Read it before selecting QA commands or handing off verification results.

`docs/QA_AUTH_TESTING.md` is the auth, control-panel session, API key, and QA credential source of truth. Read it before changing auth, session, scope, quota, API key, console, or QA workflow behavior.

Tests must be deterministic and isolated. Unit tests must not require live provider APIs, Redis, Qdrant, wall-clock sleeps, or production credentials.

Use temporary SQLite databases per test or fixture. Add concurrent tests for quota reservation/commit/release and migration changes.

Do not add `skip`, `skipif`, or `xfail`; `tests/conftest.py` intentionally rejects them. Missing prerequisites for an explicitly requested integration suite are failures, not silent passes.

Mock at system boundaries, not the function under test. Assert durable behavior: status, response shape, persisted state, emitted events, and cleanup.

Every auth change needs positive and negative cases: missing, invalid, revoked, expired, insufficient scope, wrong credential type, and forced-password-change restrictions as applicable.

Every streaming change needs success, upstream error, disconnect/cancel, quota/accounting, and single-terminal-event coverage.

### Backend gates

During development, run the changed test module:

```bash
python3 -m pytest path/to/test_file.py -q
```

Before handing off any backend behavior change:

```bash
python3 -m pytest tests/unit/ -q
python3 -m ruff check aigateway-core/src aigateway-api/src aigateway-cli/src tests
```

For auth, session, scope, quota, or admin-boundary changes, also run:

```bash
python3 -m pytest \
  tests/unit/auth/ \
  tests/unit/api/ \
  tests/unit/integration/test_p1_security_fixes.py \
  -q
```

For coverage-sensitive or broad core/API changes:

```bash
python3 -m pytest tests/unit/ \
  --cov=aigateway_core \
  --cov=aigateway_api \
  --cov-report=term-missing \
  --cov-report=xml \
  --cov-fail-under=50
```

### Frontend gates

For any control-panel behavior or API-contract change:

```bash
cd control-panel
npm test
npm run build
```

For broad frontend changes:

```bash
cd control-panel
npm run test:coverage
```

The configured frontend line-coverage threshold is 70%.

### Integration, container, and benchmark gates

Run `python3 -m pytest tests/e2e/ -q` only with an intentionally started gateway and the required test credentials/services.

Run `python3 -m pytest tests/ui/ -q` when a user journey spans the browser and backend.

For Compose changes, run `docker compose config`.

For a cached source-container rebuild, use the installer and the persisted deployment state:

```bash
BUILDKIT_PROGRESS=plain \
  bash scripts/quickstart.sh \
    --non-interactive \
    --distribution source \
    --build
```

Do not replace this with a bare `docker compose build` or `docker compose up --build` command: those commands omit `.aigateway-install.env` unless every env file and generated overlay is supplied explicitly, and may select the Lite target or wrong registry cache. Do not use `--no-cache`, `docker builder prune`, or `docker system prune -a` unless the user explicitly requests cache-bypass/cleanup or there is concrete evidence of cache corruption. See `INSTALL.md` for the rebuild and cache-miss contract.

For backend Python, dependency, Dockerfile, or baked configuration changes, rebuild the affected image, check `GET /health`, and inspect recent gateway logs for errors.

Run the benchmark suite when changing routing, caching, compression, token accounting, provider fallback, or cost behavior. Do not require live provider secrets for unrelated unit-test changes.

## Documentation and review

Update `README.md` for user-facing setup or behavior, `config.yaml.template` for configuration schema, and architecture/schema docs when their contracts change.

Update `CLAUDE.md` only for durable architecture or workflow facts, and keep it concise. Do not add task history or duplicate this file.

Code review findings must identify the concrete file and code path, explain impact, and name a verification test. Prioritize correctness, security, concurrency, resource lifecycle, and protocol compatibility over style.

Keep this file limited to durable, recurring guidance. When the user corrects a recurring project assumption, update the closest applicable `AGENTS.md`.
