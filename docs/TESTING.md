# Testing Guide

This document is the shared testing entrypoint for humans, AI coding agents, code-review models, and automated repair tools.

Use it to decide which tests to run and how to report verification. For authentication, control-panel session, API key, and QA credential flows, also read `docs/QA_AUTH_TESTING.md`.

## 1. Test selection matrix

| Change area | Required verification |
| --- | --- |
| Backend core or API behavior | Run the changed test module, then `python3 -m pytest tests/unit/ -q` before handoff. |
| Auth, browser session, API key, scope, quota, or admin boundary | Read `docs/QA_AUTH_TESTING.md`; run the relevant auth/API unit tests and include positive and negative credential-type cases. |
| Control panel behavior or API contract | `cd control-panel && npm test && npm run build`. |
| Broad control-panel behavior | `cd control-panel && npm run test:coverage` when coverage risk is material. |
| Dockerfile or Compose behavior | `docker compose config`; rebuild the affected image; verify `GET /health`; inspect recent gateway logs. |
| Config schema or defaults | Update code defaults, `config.yaml.template`, docs, and tests in the same change. |
| Routing, cache, compression, usage, cost, or provider fallback | Unit tests plus benchmark suite when behavior or measurement changes. |
| E2E or UI user journey | Run `tests/e2e/` or `tests/ui/` only when the required live services and credentials are intentionally started. |

## 2. Standard commands

Run a narrow backend test while developing:

```bash
python3 -m pytest path/to/test_file.py -q
```

Run backend unit gates before handoff:

```bash
python3 -m pytest tests/unit/ -q
python3 -m ruff check aigateway-core/src aigateway-api/src aigateway-cli/src tests
```

Run auth-sensitive gates:

```bash
python3 -m pytest \
  tests/unit/auth/ \
  tests/unit/api/ \
  tests/unit/integration/test_p1_security_fixes.py \
  -q
```

Run frontend gates:

```bash
cd control-panel
npm test
npm run build
```

Run Compose validation:

```bash
docker compose config
curl -fsS http://localhost:8000/health
```

## 3. Credential rules

Unit tests must not require live provider APIs, production credentials, Redis, Qdrant, wall-clock sleeps, or a long-running gateway.

QA API keys must be created dynamically through the admin API, injected by local environment variables, or injected by CI secrets. Do not write generated keys into repository files, snapshots, logs, docs, or committed fixtures.

Never commit raw `gw-*` API keys, provider keys, cookies, session tokens, connection strings, or secret-bearing exception output.

Use obvious placeholders in examples, such as `<created-by-admin-api-keys>` or `<provider-api-key>`.

## 4. Auth QA source of truth

Read `docs/QA_AUTH_TESTING.md` before changing or testing any of these areas:

- `/auth/*` login, logout, bootstrap, or password reset.
- `/admin/*` control-panel or management routes.
- `/admin/console/*` server-side control-panel chat adapters.
- `/v1/*` OpenAI-compatible API key authentication.
- API key creation, rotation, revocation, quota, scope, or rate limits.
- Frontend login, session persistence, or API client behavior.
- Postman, Apifox, curl, pytest, Playwright, or CI QA credentials.

The minimum auth boundary cases are:

1. Missing credentials fail.
2. Invalid credentials fail.
3. Revoked or expired credentials fail.
4. Insufficient scope fails.
5. Browser Cookie authenticates `/admin/*` only.
6. Browser Cookie does not authenticate `/v1/*`.
7. Bearer API Key authenticates `/v1/*` only.
8. Bearer API Key does not replace control-panel login.
9. `requires_password_change` sessions can only reset password or logout.

## 5. Integration and live-service rules

Do not silently skip integration suites. If a user explicitly requests an integration or E2E check and the required service or credential is missing, report it as blocked or failed, not passed.

When running live-service tests, state the exact service state:

- Gateway base URL.
- Redis and Qdrant availability.
- Whether provider keys are real, fake, or omitted.
- Whether the test created temporary QA API keys.
- Whether test data was cleaned up.

## 6. Handoff format

Every model or agent handoff must report:

- Files changed.
- Tests run, with exact commands.
- Test results.
- Tests not run and why.
- Remaining risk or follow-up verification.

Do not describe an unexecuted check as passing.
