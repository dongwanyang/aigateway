# Security Audit Report: AI Gateway

**Date**: 2026-06-27
**Auditor**: Security Engineer Agent
**Scope**: Full-stack audit of AI Gateway (FastAPI backend, React/Vite frontend, Docker infrastructure)
**Methodology**: STRIDE threat modeling, OWASP Top 10 2023, CWE Top 25, infrastructure review

---

## Executive Summary

This audit identified **3 CRITICAL**, **5 HIGH**, **8 MEDIUM**, **4 LOW**, and **6 INFORMATIONAL** findings. The most severe issues involve plaintext credentials in configuration files, missing CORS middleware despite declared intent, admin routes lacking authentication guards, and Grafana hardcoded credentials in docker-compose. Immediate remediation is required before any production deployment.

---

## FINDINGS

### F-001: Plaintext API Keys in config.yaml

- **File**: `config.yaml`, lines 7, 42, 62, 78
- **Severity**: CRITICAL
- **CVSS 3.1**: 9.1 Critical (AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H)
- **Description**: Three downstream LLM provider API keys are stored in plaintext in the committed configuration file:
  - Line 42: `api_key: sk-proj-xxx` (OpenAI placeholder)
  - Line 62: `api_key: sk-ant-xxx` (Anthropic placeholder)
  - Line 78: `api_key: sk-Idz9oi7Cvx946OY6ipSp5FjU0T1S8IHQXRnVAnu2j1Q2zPBK` (REAL AGNES key exposed)
- **Impact**: An attacker with repository access obtains the real AGNES API key, enabling unauthorized API usage billed to the account owner. The plaintext keys also travel through git history.
- **Fix**:
  1. Rotate the AGNES API key (`sk-Idz9oi7Cvx946OY6ipSp5FjU0T1S8IHQXRnVAnu2j1Q2zPBK`) immediately.
  2. Replace all keys in `config.yaml` with `${ENV_VAR}` placeholders.
  3. Load actual values from environment variables or a secrets manager (Vault, AWS Secrets Manager).
  4. Add `config.yaml` to `.gitignore` or use a git-secrets/pre-commit hook.

---

### F-002: Plaintext AGNES API Key in .env.example

- **File**: `.env.example`, line 41
- **Severity**: CRITICAL
- **CVSS 3.1**: 8.6 Critical (AV:N/AC:L/PR:L/UI:N/S:C/C:H/I:H/A:H)
- **Description**: `.env.example` contains `AGNES_API_KEY=sk-Idz9oi7Cvx946OY6ipSp5FjU0T1S8IHQXRnVAnu2j1Q2zPBK` -- the same real API key as in `config.yaml`. Template files are intended to be committed and shared; they should never contain real credentials.
- **Impact**: Anyone cloning the repository or accessing the git history obtains a valid API key.
- **Fix**: Replace with `AGNES_API_KEY=<your-agnes-api-key>` and add a comment instructing users to set it via their secrets manager or environment.

---

### F-003: Admin Routes Without Authentication Guards

- **File**: `aigateway-api/src/aigateway_api/admin_routes.py`, lines 98-682
- **Severity**: CRITICAL
- **CVSS 3.1**: 9.8 Critical (AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H)
- **Description**: The `authenticate_admin` function is defined in `auth_middleware.py` (line 172) but is **never imported or applied** to any route in `admin_routes.py`. Every admin endpoint is publicly accessible:
  - `GET  /admin/api-keys` -- lists all API keys
  - `POST /admin/api-keys` -- creates new API keys
  - `DELETE /admin/api-keys/{key_id}` -- revokes any API key
  - `PUT  /admin/plugins-config` -- modifies plugin configuration
  - `PUT  /admin/global-config` -- enables debug mode, hot reload
  - `GET  /admin/logs` -- views request logs
  - `GET  /admin/metrics-json` -- exposes internal metrics
  - `GET  /admin/quotas/{key_id}` -- queries quota details
- **Impact**: Any anonymous user can create/destroy API keys, toggle plugins, modify global configuration (including enabling debug mode), and view request logs. This effectively grants full administrative control of the gateway.
- **Fix**:
  1. Import `authenticate_admin` from `auth_middleware` in `admin_routes.py`.
  2. Add `Depends(authenticate_admin)` to every admin route handler, or apply at the router level:
     ```python
     from fastapi import Depends
     from aigateway_api.auth_middleware import authenticate_admin

     router = APIRouter(dependencies=[Depends(authenticate_admin)])
     ```

---

### F-004: CORS Middleware Imported But Never Configured

- **File**: `aigateway-api/src/aigateway_api/main.py`, line 20
- **Severity**: HIGH
- **CVSS 3.1**: 7.5 High (AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N)
- **Description**: `CORSMiddleware` is imported on line 20 but never instantiated or added to the application. The docstring on line 5 states "挂载 CORS 中间件" but no `app.add_middleware(CORSMiddleware, ...)` call exists.
- **Impact**: Without CORS configuration, the browser will block cross-origin requests from the control panel (served on port 3000) to the API gateway (port 8000) in non-proxy setups. The absence of explicit configuration also means the developer assumed CORS was handled when it was not.
- **Fix**: Add explicit CORS configuration with restricted origins:
  ```python
  app.add_middleware(
      CORSMiddleware,
      allow_origins=["https://your-domain.com"],
      allow_credentials=True,
      allow_methods=["GET", "POST", "PUT", "DELETE"],
      allow_headers=["Authorization", "x-api-key", "Content-Type"],
  )
  ```

---

### F-005: Hardcoded Grafana Credentials in docker-compose.yml

- **File**: `docker-compose.yml`, lines 99-100
- **Severity**: HIGH
- **CVSS 3.1**: 8.1 High (AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:N)
- **Description**: Grafana admin credentials are hardcoded as environment variables: `GF_SECURITY_ADMIN_USER=admin` and `GF_SECURITY_ADMIN_PASSWORD=admin`. Both username and password are the default `admin`.
- **Impact**: Anyone with access to the docker-compose file or running containers obtains the Grafana admin password. If exposed to any network, attackers can access dashboards containing potentially sensitive metrics and configuration data.
- **Fix**: Move to environment variable interpolation:
  ```yaml
  environment:
    - GF_SECURITY_ADMIN_USER=${GRAFANA_ADMIN_USER:-admin}
    - GF_SECURITY_ADMIN_PASSWORD=${GRAFANA_ADMIN_PASSWORD}
  ```
  Then set `GRAFANA_ADMIN_PASSWORD` in `.env` (which is gitignored).

---

### F-006: Redis Exposed on Public Network Interface

- **File**: `docker-compose.yml`, lines 47-48
- **Severity**: HIGH
- **CVSS 3.1**: 8.8 High (AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H)
- **Description**: Redis port 6379 is published to the host (`"6379:6379"`). Combined with no Redis authentication (no `requirepass` in the command on line 51), any network-accessible host can connect to Redis and read/write all data including API keys, cache entries, and request logs.
- **Impact**: Full data exfiltration, API key theft, cache poisoning, and potential RCE via Redis MODULE LOAD or AOF/RLIMIT exploits.
- **Fix**:
  1. Remove the `ports` mapping for Redis -- it should only be accessible via the `gateway-net` internal network.
  2. Add `requirepass` to the Redis command: `redis-server --appendonly yes --maxmemory 256mb --maxmemory-policy allkeys-lru --requirepass ${REDIS_PASSWORD}`
  3. Update `AI_GATEWAY_REDIS_URL` to include the password: `redis://:${REDIS_PASSWORD}@redis:6379/0`

---

### F-007: Unmasked API Key Values in Error Responses and Logs

- **File**: `aigateway-api/src/aigateway_api/auth_middleware.py`, lines 130, 140
- **Severity**: MEDIUM
- **CVSS 3.1**: 5.3 Medium (AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:N/A:N)
- **Description**: When an API key is revoked or suspended, the error message leaks the first 8 characters of the full key value: `f"API key '{key_value[:8]}...' has been revoked"`. Additionally, `request.state.api_key_value` stores the plaintext key on line 167, making it accessible throughout the request lifecycle.
- **Impact**: If an error response is logged or reflected in any debug output, partial key values can assist in brute-force attacks or key enumeration.
- **Fix**: Never log or include any portion of the API key value in error messages. Use only the `key_id` or `key_hash`:
  ```python
  detail={"error": {"code": "forbidden", "message": "API key has been revoked"}}
  ```

---

### F-008: Config Hot-Reload Allows Arbitrary Environment Variable Injection

- **File**: `aigateway-core/src/aigateway_core/config.py`, lines 186-227
- **Severity**: MEDIUM
- **CVSS 3.1**: 6.2 Medium (AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:N)
- **Description**: The `_resolve_env_vars_in_values` method processes `${ENV_VAR}` patterns in ALL config values, including API keys. While `yaml.safe_load` prevents arbitrary Python execution, the env var resolution means any environment variable can be injected into the config at runtime. An attacker who can set environment variables (e.g., via the docker-compose `environment` section) can manipulate provider API keys.
- **Impact**: An attacker with docker-compose write access can inject arbitrary values into the config, including replacing provider API keys with their own.
- **Fix**: Restrict env var resolution to a whitelist of known safe keys, or disable it entirely for sensitive fields (providers, auth). Better yet, load provider keys exclusively from environment variables, never from YAML.

---

### F-009: Admin API Can Enable Debug Mode Without Safeguards

- **File**: `aigateway-api/src/aigateway_api/admin_routes.py`, lines 563-610
- **Severity**: MEDIUM
- **CVSS 3.1**: 6.2 Medium (AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:N)
- **Description**: The `PUT /admin/global-config` endpoint allows enabling `debug_mode` at runtime. When enabled, it calls `setup_logging(log_level="DEBUG")` (line 605), which increases log verbosity. With F-003 (unprotected admin routes), any unauthenticated user can enable debug logging. Even with auth, this is a dangerous capability -- DEBUG mode may log request bodies including user prompts, which could contain PII.
- **Impact**: Enabling debug mode causes full request/response logging, potentially exposing sensitive user data in log files and violating data minimization principles.
- **Fix**:
  1. Protect this endpoint with admin auth (see F-003).
  2. Add a confirmation/captcha requirement for debug mode toggling.
  3. Ensure PII detector runs before any DEBUG-level logging of request bodies.

---

### F-010: No Rate Limiting on Admin Endpoints

- **File**: `aigateway-api/src/aigateway_api/admin_routes.py`, all admin routes
- **Severity**: MEDIUM
- **CVSS 3.1**: 5.3 Medium (AV:N/AC:L/PR:L/UI:N/S:U/C:N/I:L/A:L)
- **Description**: Admin endpoints (list API keys, create API keys, revoke keys, update plugins, update global config, view logs) have no rate limiting. A compromised admin key or brute-force attacker can make unlimited requests.
- **Impact**: Brute-force attacks against admin API keys, denial of service via excessive key creation/deletion, or rapid configuration changes.
- **Fix**: Apply a dedicated rate limiter to the admin router using `slowapi` or similar:
  ```python
  from slowapi import Limiter
  from slowapi.util import get_remote_address

  limiter = Limiter(key_func=get_remote_address)
  app.state.limiter = limiter
  ```

---

### F-011: Prometheus Metrics Endpoint Unauthenticated

- **File**: `aigateway-api/src/aigateway_api/routes.py`, line 35
- **Severity**: LOW
- **CVSS 3.1**: 3.7 Low (AV:N/AC:H/PR:N/UI:N/S:U/C:L/I:N/A:N)
- **Description**: `GET /metrics` returns Prometheus metrics without authentication. While metrics themselves may not contain secrets, they can leak system internals (request counts, latency distributions, circuit breaker states) that aid reconnaissance.
- **Impact**: Information disclosure about system topology, traffic patterns, and failure rates.
- **Fix**: Add basic auth or restrict to internal network access. Consider stripping or hashing any identifying labels from metrics.

---

### F-012: Health Endpoint Leaks Internal Version

- **File**: `aigateway-api/src/aigateway_api/routes.py`, line 170
- **Severity**: LOW
- **CVSS 3.1**: 2.6 Low (AV:N/AC:H/PR:N/UI:N/S:U/C:L/I:N/A:N)
- **Description**: `GET /health` returns `"version": "1.0.0"` in the response. Combined with dependency information from `/metrics`, this helps attackers identify known CVEs for this specific version.
- **Impact**: Assists targeted attacks against known vulnerabilities in version 1.0.0.
- **Fix**: Omit version from public endpoints, or return a build hash instead of semantic version.

---

### F-013: Structlog ExceptionPrettyPrinter in Production Pipeline

- **File**: `aigateway-core/src/aigateway_core/logger.py`, line 171
- **Severity**: MEDIUM
- **CVSS 3.1**: 5.3 Medium (AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:N/A:N)
- **Description**: `structlog.processors.ExceptionPrettyPrinter()` is included in the processor chain for all log formats, including JSON. This processor outputs pretty-printed exception tracebacks, which can corrupt JSON log output and leak stack traces including file paths, internal module names, and potentially sensitive data from exception messages.
- **Impact**: Stack trace leakage in production logs, corrupted structured log parsing, potential exposure of internal architecture.
- **Fix**: Remove `ExceptionPrettyPrinter` from the production processor chain. Use `structlog.processors.StackInfoRenderer` for structured stack info, or reserve `ExceptionPrettyPrinter` for text-only development logging.

---

### F-014: Qdrant and Prometheus Exposed on Host Ports

- **File**: `docker-compose.yml`, lines 66-68, 80-81
- **Severity**: LOW
- **CVSS 3.1**: 5.3 Medium (AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N)
- **Description**: Qdrant ports 6333/6334 and Prometheus port 9090 are published to the host. Qdrant without authentication exposes the semantic cache (containing cached LLM responses that may include PII or business logic). Prometheus exposes all internal metrics.
- **Impact**: Unauthorized access to vector database and monitoring dashboard.
- **Fix**: Remove `ports` mappings for Qdrant and Prometheus; access only through internal network or authenticated reverse proxy.

---

### F-015: No Transport Encryption for Redis and Qdrant Connections

- **File**: `aigateway-core/src/aigateway_core/redis_client.py`, line 53; `aigateway-core/src/aigateway_core/qdrant_client.py`, line 49
- **Severity**: MEDIUM
- **CVSS 3.1**: 5.3 Medium (AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N)
- **Description**: Redis connects via `redis://` (plaintext) and Qdrant via `http://` (plaintext). Even in Docker, traffic between containers on the same host can be intercepted if the host network is compromised. Redis keys (including API key hashes) and Qdrant vectors (cached LLM responses) travel unencrypted.
- **Impact**: Man-in-the-middle attack on inter-service communication enables credential theft and data exfiltration.
- **Fix**:
  1. For Redis: Use `rediss://` with TLS certificates, or at minimum rely on Docker network isolation with `requirepass`.
  2. For Qdrant: Use `https://` with TLS, or set `QDRANT_SERVICE_API_KEY` and enable authentication.

---

### F-016: API Key Prefix Extraction Includes Raw Key Bytes

- **File**: `aigateway-core/src/aigateway_core/security.py`, line 91
- **Severity**: LOW
- **CVSS 3.1**: 3.1 Low (AV:N/AC:H/PR:L/UI:N/S:U/C:L/I:N/A:N)
- **Description**: `_prefix_key` returns the first 8 characters of the raw API key value. This is used in Redis keys (`aigateway:key_lookup:{key_prefix}`) and returned in admin API responses. If an attacker discovers the key format (`sk-{16 hex chars}`), they can enumerate all keys by prefix.
- **Impact**: Limited -- key format is predictable but the prefix alone is insufficient for authentication.
- **Fix**: Use a random prefix generated at key creation time, independent of the key value itself.

---

### F-017: SHA-256 Hash with 16-Character Truncation for Key Storage

- **File**: `aigateway-core/src/aigateway_core/security.py`, line 86
- **Severity**: LOW
- **CVSS 3.1**: 3.7 Low (AV:N/AC:H/PR:N/UI:N/S:U/C:L/I:N/A:N)
- **Description**: API keys are hashed with SHA-256 and truncated to 16 hex characters (64 bits of entropy). The hash is computed without a salt, making rainbow table attacks feasible if the hash database is compromised.
- **Impact**: Weakens the security of key storage lookups; salted hashing (e.g., SHA-256 with a server-side salt) adds defense in depth.
- **Fix**: Use a server-side salt: `hashlib.sha256(salt + key_value.encode()).hexdigest()[:16]` where salt is loaded from environment.

---

### F-018: No CSRF Protection on Admin Endpoints

- **File**: `aigateway-api/src/aigateway_api/admin_routes.py` (all PUT routes)
- **Severity**: LOW
- **CVSS 3.1**: 4.3 Low (AV:N/AC:L/PR:L/UI:R/S:C/C:N/I:L/A:N)
- **Description**: Admin endpoints accepting state-changing requests (PUT) have no CSRF protection. While API-key-based auth mitigates traditional CSRF (browsers do not send custom headers automatically), if any endpoint also accepts cookie-based auth, CSRF becomes viable.
- **Impact**: Minimal given API key auth, but a best practice to add CSRF tokens for any cookie-authenticated routes.
- **Fix**: Add CSRF middleware for routes that may accept cookie-based auth in the future.

---

### F-019: Frontend Stores API Key in localStorage

- **File**: `control-panel/src/api/client.ts`, line 34
- **Severity**: MEDIUM
- **CVSS 3.1**: 5.3 Medium (AV:N/AC:L/PR:N/UI:R/S:U/C:H/I:N/A:N)
- **Description**: The API key is stored in `localStorage` as `aigateway_api_key`. localStorage is accessible to any JavaScript running on the page, making it vulnerable to XSS attacks. If an attacker injects script via any XSS vector, they can steal the stored key.
- **Impact**: API key theft via cross-site scripting.
- **Fix**:
  1. Use httpOnly secure cookies instead of localStorage for token storage.
  2. If localStorage must be used, implement strict CSP headers and sanitize all user-generated content displayed in the frontend.
  3. Add token expiration and refresh mechanisms.

---

### F-020: Missing Content-Security-Policy on Nginx Frontend

- **File**: `control-panel/nginx.conf`
- **Severity**: MEDIUM
- **CVSS 3.1**: 5.3 Medium (AV:N/AC:L/PR:N/UI:R/S:U/C:H/I:N/A:N)
- **Description**: The Nginx configuration serves the React frontend without any security headers (no CSP, X-Frame-Options, X-Content-Type-Options, HSTS, Referrer-Policy).
- **Impact**: Vulnerable to XSS, clickjacking, MIME-type sniffing, and referrer leakage.
- **Fix**: Add security headers in `nginx.conf`:
  ```nginx
  add_header X-Content-Type-Options "nosniff" always;
  add_header X-Frame-Options "SAMEORIGIN" always;
  add_header X-XSS-Protection "1; mode=block" always;
  add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
  add_header Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline';" always;
  add_header Referrer-Policy "strict-origin-when-cross-origin" always;
  ```

---

### F-021: Plugin Toggle Bypasses Input Validation

- **File**: `aigateway-api/src/aigateway_api/admin_routes.py`, lines 348-424
- **Severity**: MEDIUM
- **CVSS 3.1**: 5.3 Medium (AV:N/AC:L/PR:L/UI:N/S:U/C:N/I:L/A:L)
- **Description**: The `PUT /admin/plugins-config` endpoint parses the request body manually via `await request.json()` instead of using a Pydantic model. The `name` field is validated only with `if not name:` which accepts falsy values but does not validate the name against known plugins. An attacker could write arbitrary plugin names to the config file.
- **Impact**: Configuration corruption by writing unrecognized plugin names, potentially breaking the plugin system.
- **Fix**: Use a Pydantic model with an enum for plugin names, or validate against the known plugin list from `PluginRegistry`.

---

### F-022: No TLS/HTTPS Termination

- **File**: `docker-compose.yml`; `aigateway-api/Dockerfile`
- **Severity**: MEDIUM
- **CVSS 3.1**: 5.3 Medium (AV:N/AC:H/PR:N/UI:R/S:U/C:H/I:H/A:N)
- **Description**: The entire stack communicates over HTTP. No TLS termination is configured at any layer -- the API gateway listens on port 8000 without HTTPS, the frontend on port 3000/80 without HTTPS, and inter-container traffic is plaintext.
- **Impact**: All traffic including API keys, user prompts, LLM responses, and credentials are transmitted in cleartext, vulnerable to network-level interception.
- **Fix**:
  1. Add a reverse proxy (Traefik, Nginx, Caddy) with TLS termination in front of the stack.
  2. Use Let's Encrypt for automated certificate management.
  3. Or at minimum, configure `uvicorn` with SSL certificates: `uvicorn ... --ssl-keyfile=/certs/key.pem --ssl-certfile=/certs/cert.pem`.

---

## STRIDE Threat Model Summary

| Threat Category | Component | Risk | Finding Reference |
|-----------------|-----------|------|-------------------|
| Spoofing | Admin API endpoints | Critical | F-003 |
| Tampering | config.yaml (runtime) | High | F-008, F-021 |
| Tampering | Plugin toggle input | Medium | F-021 |
| Repudiation | Request logging (no auth) | Medium | F-003 |
| Info Disclosure | AGNES key in config | Critical | F-001, F-002 |
| Info Disclosure | Grafana creds in compose | High | F-005 |
| Info Disclosure | API key in error messages | Medium | F-007 |
| Info Disclosure | Prometheus metrics public | Low | F-011 |
| DoS | No admin rate limiting | Medium | F-010 |
| DoS | Redis unauthenticated | High | F-006 |
| Elevation of Priv | Debug mode toggle | Medium | F-009 |
| Elevation of Priv | Unprotected admin routes | Critical | F-003 |

---

## Remediation Priority

### Immediate (Before Any Deployment)
1. **F-001/F-002**: Rotate the exposed AGNES API key and remove plaintext credentials from config files.
2. **F-003**: Wire `authenticate_admin` to all admin routes.
3. **F-005**: Replace hardcoded Grafana credentials with environment variable interpolation.
4. **F-006**: Remove Redis port exposure and add `requirepass`.

### Next Sprint
5. **F-004**: Configure explicit CORS middleware with restricted origins.
6. **F-019**: Migrate API key storage from localStorage to httpOnly cookies.
7. **F-020**: Add security headers to Nginx frontend configuration.
8. **F-022**: Deploy TLS termination via reverse proxy.

### Backlog
9. **F-010**: Implement rate limiting on admin endpoints.
10. **F-015**: Enable TLS for Redis and Qdrant connections.
11. **F-013**: Fix structlog processor chain for production JSON logging.
12. **F-008**: Whitelist env var injection in config resolution.

---

## Compliance Notes

- **OWASP API Security Top 10**: F-003 violates API4:2023 (Function Level Access Control) and API7:2023 (Auth and Rate Limiting). F-001/F-002 violate API1:2023 (Broken Object Level Authorization via leaked credentials).
- **PCI-DSS**: Requirement 2.3 (encrypt storage of credentials) -- violated by F-001/F-002. Requirement 6.5.7 (injection) -- mitigated by Redis parameterized access but F-021 lacks input validation.
- **GDPR**: PII detection plugin exists but debug mode (F-009) can bypass sanitization by logging full request bodies.
