# AI Gateway Framework — Design Spec

> Date: 2026-06-27
> Status: Approved (rev.2 — incorporating security, observability, and resilience feedback)
> Tech Stack: Python + FastAPI, LiteLLM, LangChain, LlamaIndex, Qdrant, Redis, Prometheus + Grafana, React + Vite, OpenTelemetry

## Problem Statement

Build a token-optimizing AI Gateway that sits between clients and LLM providers.
It compresses prompts, caches responses, routes models, optimizes tool calls,
and provides observability — all as a drop-in OpenAI API compatible proxy.

**Four usage forms for different users:**
1. **API Gateway** — drop-in OpenAI proxy for existing apps
2. **CLI Tool** — `aigateway chat` for terminal-based interaction
3. **IDE Extension** — VS Code / Cursor plugin that intercepts AI requests
4. **Docker Compose** — one-command deployment of all three

## Core Principle

**Reuse, don't reinvent.** Leverage existing mature open-source tools:

| Capability | Open-Source Tool |
|---|---|
| Multi-model gateway & routing | **LiteLLM** (Router, CostTracker, Completion) |
| Conversation summary & memory | **LangChain** (ConversationSummaryBufferMemory) |
| RAG & document retrieval | **LlamaIndex** + **LangChain** (QdrantVectorStore, CrossEncoderReranker) |
| Vector cache | **Qdrant** + LangChain integration |
| KV cache | **Redis** + LangChain RedisCache |
| Embeddings | **sentence-transformers** (local) / OpenAI API (configurable) |
| Local inference (optional) | **vLLM** |
| Metrics | **Prometheus** + Grafana |
| Distributed tracing | **OpenTelemetry** (Python SDK) |
| Structured logging | **structlog** (JSON output) |

What we build: **Pipeline orchestration**, **four usage adapters**, **plugin glue code**, **security layer**, and **control dashboard**.

## Architecture

```
                    ┌─────────────────────────────────────┐
                    │       AI Gateway Core (lib)         │
                    │   Pipeline Engine + Plugin Registry │
                    │   LiteLLM + LangChain + LlamaIndex  │
                    └────┬──────────┬──────────┬──────────┘
                       │          │          │
              ┌────────┘    ┌─────┘    ┌─────┘
              ▼             ▼          ▼
        ┌──────────┐  ┌──────────┐  ┌──────────┐
        │ API      │  │ CLI      │  │ IDE      │
        │ Gateway  │  │ Tool     │  │ Extension│
        │          │  │          │  │          │
        │ 改       │  │ 终端直   │  │ VS Code  │
        │ base_url │  │ 聊       │  │ / Cursor │
        │ + api_   │  │          │  │ 扩展     │
        │ key      │  │ pip      │  │          │
        │          │  │ install  │  │ 自动     │
        │ 现有     │  │ &&       │  │ 拦截     │
        │ 应用     │  │ chat     │  │ 所有     │
        │ 自动     │  │          │  │ AI 请求  │
        │ 接入     │  │ 零代码   │  │          │
        │          │  │ 改动     │  │ 零配置   │
        └──────────┘  └──────────┘  └──────────┘
                │             │             │
                └─────────────┴─────────────┘
                              │
                    ┌─────────────────┐
                    │  Pipeline Engine│
                    │                 │
                    │  ┌───────────┐  │
                    │  │ Auth MW   │  │ API Key + quota check
                    │  └────┬──────┘  │
                    │  ┌────┴──────┐  │
                    │  │ Prompt    │  │ Compress + PII sanitize
                    │  │ Compress  │  │
                    │  └────┬──────┘  │
                    │  ┌────┴──────┐  │
                    │  │ Prompt    │──┼── hit → return cached (short-circuit)
                    │  │ Cache     │  │
                    │  └────┬──────┘  │
                    │  ┌────┴──────┐  │
                    │  │ Semantic  │──┼── hit → return cached (short-circuit)
                    │  │ Cache     │  │
                    │  └────┬──────┘  │
                    │  ┌────┴──────┐  │
                    │  │ Conv Comp │  │ Long conversation summary
                    │  └────┬──────┘  │
                    │  ┌────┴──────┐  │
                    │  │ RAG Retr  │  │ Doc retrieval + rerank
                    │  └────┬──────┘  │
                    │  ┌────┴──────┐  │
                    │  │ Model     │  │ LiteLLM Router (cost/speed/quality)
                    │  │ Router    │  │ + fallback + retries
                    │  └────┬──────┘  │
                    │  ┌────┴──────┐  │
                    │  │ MCP Opt   │  │ Tool result cropping
                    │  └────┬──────┘  │
                    │  ┌────┴──────┐  │
                    │  │ LiteLLM   │  │ Downstream LLM call
                    │  │ Complete  │  │
                    │  └────┬──────┘  │
                    │  ┌────┴──────┐  │
                    │  │ Resp      │  │ JSON format / length limit
                    │  │ Format    │  │
                    │  └────┬──────┘  │
                    │  ┌────┴──────┐  │
                    │  │ OTel +    │  │ Trace ID propagation
                    │  │ Metrics   │  │ Prometheus + OpenTelemetry
                    │  └───────────┘  │
                    └────────┬────────┘
                             │
                    ┌────────┴────────┐
                    │  Downstream     │
                    │  LLM APIs       │
                    │  OpenAI/Anthr/  │
                    │  Gemini/Bedrock │
                    └─────────────────┘
```

## Project Structure

```
gateway/
├── docker-compose.yml
├── .env.example
│
├── aigateway-core/             # Shared library (core pipeline engine)
│   ├── pyproject.toml
│   ├── src/aigateway_core/
│   │   ├── pipeline.py         # Async plugin pipeline engine
│   │   ├── plugin_registry.py  # Plugin registration, ordering & dependency validation
│   │   ├── config.py           # YAML config loader (env var override, hot reload)
│   │   ├── litellm_bridge.py   # LiteLLM wrapper (router, completion, cost)
│   │   ├── context.py          # PipelineContext — shared state across plugins
│   │   ├── security.py         # API Key auth, quota manager, PII handler
│   │   ├── tracing.py          # OpenTelemetry trace ID injection
│   │   └── logger.py           # Structured JSON logging via structlog
│   ├── tests/
│   └── requirements.txt
│
├── aigateway-api/              # API Gateway form
│   ├── src/aigateway_api/
│   │   ├── main.py             # FastAPI app, /v1/* endpoints
│   │   ├── openai_compat.py    # OpenAI API compatibility layer
│   │   └── auth_middleware.py  # API Key validation middleware
│   ├── Dockerfile
│   └── requirements.txt
│
├── aigateway-cli/              # CLI Tool form
│   ├── src/aigateway_cli/
│   │   ├── __main__.py         # entrypoint: aigateway
│   │   ├── chat.py             # Interactive chat (rich library)
│   │   ├── run.py              # Single-shot request
│   │   └── session.py          # Session management
│   └── requirements.txt
│
├── aigateway-ide/              # IDE Extension form
│   ├── src/
│   │   ├── extension.ts        # VS Code / Cursor extension
│   │   └── http-proxy.ts       # Request interception proxy
│   ├── package.json
│   └── tsconfig.json
│
├── control-panel/              # Web Dashboard
│   ├── package.json
│   ├── vite.config.ts
│   ├── src/
│   │   ├── api/                # API client for gateway admin endpoints
│   │   ├── components/         # Reusable UI (charts, toggles, tables)
│   │   └── pages/
│   │       ├── Overview.tsx    # QPS, latency, cost overview
│   │       ├── Plugins.tsx     # Plugin toggle & config management
│   │       ├── Costs.tsx       # Cost analysis (LiteLLM CostTracker data)
│   │       ├── Quotas.tsx      # Budget/quota management per API key
│   │       ├── Cache.tsx       # Cache hit rates
│   │       └── Logs.tsx        # Request logs (with trace IDs)
│   ├── Dockerfile
│   └── nginx.conf
│
└── infra/
    ├── prometheus/prometheus.yml
    ├── grafana/
    │   ├── datasources.yml
    │   └── dashboards/         # Pre-built dashboard JSON files
    └── init/qdrant_collections/
```

## Plugin System

### Base Interface

Each plugin implements `BasePlugin` with two methods:
- `async process(context: PipelineContext) -> PipelineContext` — transform request/response in-place
- `enabled(config: dict) -> bool` — check if plugin is active

### PipelineContext Schema

`PipelineContext` carries shared state across the pipeline. The `extra` field uses a strict namespace convention to prevent key collisions:

```python
class PipelineContext:
    request: dict                           # Original OpenAI-format request
    response: Optional[str] = None          # Set by cache plugins on hit
    should_stop: bool = False               # Set to True to short-circuit
    should_stream: bool = False             # True for SSE streaming responses
    trace_id: str                           # OpenTelemetry trace ID
    request_id: str                         # Unique request ID (UUID)
    user_id: Optional[str] = None           # From API key
    extra: dict = {}                        # Plugin-specific data, namespaced
```

**Namespace convention for `extra`:** Each plugin uses a unique prefix:
- `extra['prompt_compress']['original_length']`, `extra['prompt_compress']['compressed_prompt']`
- `extra['semantic_cache']['similarity_score']`, `extra['semantic_cache']['cached_response']`
- `extra['rag']['reranked_docs']`, `extra['mcp']['stored_object_key']`

This prevents two plugins from accidentally overwriting each other's data.

### Short-Circuit with Streaming

When a cache plugin hits and the original request was a streaming request:
- The cached response is **chunked and streamed** via SSE to simulate real LLM generation
- `context.should_stream` is checked; if True, the response is split into chunks with configurable chunk size and delay (e.g., 20ms per chunk)
- This ensures the client sees no difference between a cache hit and a real LLM response

### Dependency Declaration

Plugins can declare explicit dependencies to prevent misconfiguration:

```yaml
plugins:
  - name: semantic_cache
    enabled: true
    depends_on: [prompt_compress]   # Must run after prompt compression
    config: { threshold: 0.95, ttl: 86400 }
```

On startup, `plugin_registry.py` validates the declared dependency graph. If a dependency is disabled or placed before the required plugin, a warning/error is logged and the plugin is skipped.

### Hot Reload

Config file changes trigger automatic reload:
- `config.py` watches `config.yaml` via `watchdog` library
- **Atomic swap**: New config is loaded into a temporary object, validated, then atomically swapped into the global config. Active requests are not interrupted; they complete with the old config. New requests use the updated config.
- Changes are applied without restarting the service
- Active requests are not interrupted; new requests use the updated config

## Security Layer

### API Key Authentication

```python
# auth_middleware.py
async def validate_api_key(request: Request) -> Optional[str]:
    """Validate API key from Authorization header or x-api-key header."""
    api_key = request.headers.get("x-api-key") or \
              request.headers.get("Authorization", "").replace("Bearer ", "")
    if not api_key or not key_store.validate(api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")
    return api_key
```

- Key store backed by Redis (for distributed deployments) or in-memory (single instance)
- Keys created/revoked via Control Panel or admin API
- **Multi-instance sync**: When a key is created/revoked/updated, Redis Pub/Sub broadcasts the change event to all gateway instances, ensuring near-real-time consistency
- Multi-tenant isolation: each key maps to a `user_id`; cache, quotas, and logs are scoped per user

### PII Handling Performance

PII detection uses regex patterns on the prompt text. For long prompts (>10KB):
- Detection runs in a thread pool executor to avoid blocking the async event loop
- For prompts >50KB, only the first 10KB and last 10KB are scanned (sampling)
- This balances security against performance — most PII appears at the beginning or end of prompts

#### PII Pattern Catalog

Patterns are organized by category, aligned with Azure Language Service PII taxonomy and supplemented with Chinese-specific patterns. Each pattern is designed for regex-based detection and masking.

```yaml
pii_strategy: "sanitize"  # Options: "sanitize" | "reject" | "hash"

pii_patterns:
  # ── Contact Information ──────────────────────────────────
  email:
    pattern: r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}'
    mask: "[EMAIL_REDACTED]"
    description: "Global email addresses"

  phone:
    patterns:
      # Chinese mainland mobile: 1xx-xxxx-xxxx (11 digits, starts with 1)
      cn_mobile: r'\b1[3-9]\d{9}\b'
      # Chinese landline: xxx-xxxxxxxx or xxxx-xxxxxxxx
      cn_landline: r'\b(0\d{2,3}-)?\d{7,8}\b'
      # US/E164 international: +1-xxx-xxx-xxxx or +44 20 7946 0958
      intl_e164: r'\+\d{1,3}[\s\-]?\d{4,14}[\s\-]?\d{4,14}'
      # Generic 10+ digit phone
      generic: r'\b\d{10,}\b'
    mask: "[PHONE_REDACTED]"

  url:
    pattern: r'https?://[^\s<>"{}|\\^`[\]]+'
    mask: "[URL_REDACTED]"
    description: "HTTP/HTTPS URLs"

  ip_address:
    patterns:
      ipv4: r'\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b'
      ipv6: r'(?i)(?:(?:[0-9a-f]{1,4}:){7}[0-9a-f]{1,4}|(?:[0-9a-f]{1,4}:){1,7}:|(?:[0-9a-f]{1,4}:){1,6}:[0-9a-f]{1,4}|(?:[0-9a-f]{1,4}:){1,5}(?::[0-9a-f]{1,4}){1,2}|(?:[0-9a-f]{1,4}:){1,4}(?::[0-9a-f]{1,4}){1,3}|(?:[0-9a-f]{1,4}:){1,3}(?::[0-9a-f]{1,4}){1,4}|(?:[0-9a-f]{1,4}:){1,2}(?::[0-9a-f]{1,4}){1,5}|[0-9a-f]{1,4}:(?::[0-9a-f]{1,4}){1,6}|:(?::[0-9a-f]{1,4}){1,7})'
    mask: "[IP_REDACTED]"

  # ── Government Issued IDs ────────────────────────────────
  # Chinese mainland patterns
  cn_id_card:
    pattern: r'\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b'
    mask: "[CN_ID_REDACTED]"
    description: "18-digit Chinese resident ID (GB 11643-1999). Format: 6-digit address + 8-digit birthdate + 3-digit sequence + checksum. Last digit can be X."
    validation: "Luhn-like modulo-11 check"

  cn_id_card_old:
    pattern: r'\b[1-9]\d{7}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}\b'
    mask: "[CN_ID_OLD_REDACTED]"
    description: "15-digit legacy Chinese ID (pre-1999). Format: 6-digit address + 6-digit birthdate (YYMMDD) + 3-digit sequence."

  cn_social_security:
    pattern: r'\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b'
    mask: "[CN_SSN_REDACTED]"
    description: "Chinese social security number — shares format with resident ID card."

  cn_passport:
    patterns:
      # Chinese passport: G + 9 digits, or P + 9 digits
      gd: r'\b[GpP]\d{8,9}\b'
      # Business passport: EA, EB, EC... etc.
      business: r'\b[EA-HK][A-Z]\d{7,8}\b'
    mask: "[CN_PASSPORT_REDACTED]"

  cn_driver_license:
    pattern: r'\b[1-9]\d{14}\b'
    mask: "[CN_DL_REDACTED]"
    description: "15/18-digit Chinese driver license number."

  cn_hukou:
    pattern: r'\b[1-9]\d{5}\d{8}[\dxX]\b'
    mask: "[CN_HUKOU_REDACTED]"
    description: "Household registration number (similar to ID card format)."

  cn_tax_id:
    pattern: r'\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b'
    mask: "[CN_TAX_REDACTED]"
    description: "Chinese tax ID — shares format with resident ID."

  # US patterns
  us_ssn:
    pattern: r'\b\d{3}-?\d{2}-?\d{4}\b'
    mask: "[SSN_REDACTED]"
    description: "US Social Security Number. Formats: 123-45-6789, 123456789."
    validation: "First 3 digits not 000, 666, or 900-999; last 4 not 0000"

  us_passport:
    pattern: r'\b\d{9}\b'
    mask: "[US_PASSPORT_REDACTED]"
    description: "9-digit US passport number (ambiguous — only match in context)."

  us_drivers_license:
    pattern: r'\b[A-Z]\d{7,8}\b'
    mask: "[US_DL_REDACTED]"
    description: "US driver license (varies by state)."

  # ── Financial Information ────────────────────────────────
  credit_card:
    pattern: r'\b(?:4[0-9]{2}|5[1-5][0-9]|6(?:011|5[0-9][0-9])|3[47][0-9]{2})[-\s]?(\d{3,4}[-\s]?){2}\d{3,4}\b'
    mask: "[CC_REDACTED]"
    description: "Visa (4xxx), MasterCard (51xx-55xx), Amex (34xx/37xx), Discover (6011/65xx). 13-19 digits."
    validation: "Luhn algorithm"

  iban:
    pattern: r'\b[A-Z]{2}\d{2}[\s]?[A-Z0-9]{4}[\s]?(?:[A-Z0-9]{4}[\s]?){2,}\.[A-Z0-9]{1,4}'
    mask: "[IBAN_REDACTED]"
    description: "International Bank Account Number (up to 34 chars)."

  swift_code:
    pattern: r'\b[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?\b'
    mask: "[SWIFT_REDACTED]"
    description: "SWIFT/BIC code. 8 or 11 characters."

  # ── Addresses ──────────────────────────────────────────
  cn_address:
    pattern: r'\b[一-龥]{2,}(省|市|自治区|壮族|回族|维吾尔|蒙古|自治州|地区|县|市|区|镇|乡|街道|路|号|栋|室|小区|大厦|广场|村)[一-龥\s\w\d\-./]{5,}'
    mask: "[ADDRESS_REDACTED]"
    description: "Chinese addresses contain administrative divisions (省/市/区/镇/路/号/栋/室) mixed with CJK characters."

  us_address:
    pattern: r'\b\d{1,5}[\s]+(?:[A-Z][a-z]+[\s]+){1,3}(St|Street|Ave|Avenue|Blvd|Boulevard|Rd|Road|Dr|Drive|Ln|Lane|Way|Ct|Court|Plz|Place|Cir|Circle|Ter|Terrace)[\.,]?'
    mask: "[ADDRESS_REDACTED]"
    description: "US street addresses with common suffixes."

  zip_code:
    patterns:
      us: r'\b\d{5}(?:-\d{4})?\b'
      cn: r'\b[1-9]\d{5}\b'
      uk: r'\b[A-Z]{1,2}\d{1,2}[A-Z]?\s?\d[A-Z]{2}\b'
    mask: "[ZIP_REDACTED]"

  # ── Credentials & Secrets ────────────────────────────────
  password:
    pattern: r'(?:password|passwd|pwd|pass|pw|secret|token|api[_\-]?key|apikey|access[_\-]?key|auth[_\-]?token|bearer)\s*[:=]\s*["\']?[^\s"\']{6,}["\']?'
    mask: "[CREDENTIAL_REDACTED]"
    description: "Credentials appearing as key=value pairs. Matches common variable names and config patterns."

  private_key:
    pattern: r'-----BEGIN\s+(?:RSA\s+)?PRIVATE\s+KEY-----'
    mask: "[PRIVATE_KEY_REDACTED]"
    description: "PEM-encoded private key headers."

  aws_access_key:
    pattern: r'\b[A-Z0-9]{20}\b'
    mask: "[AWS_KEY_REDACTED]"
    description: "AWS Access Key ID (20 uppercase alphanumeric chars). Match in context of 'AKIA' prefix."

  connection_string:
    pattern: r'(?:mongodb(?:\+srv)?|mysql|postgres(?:ql)?|redis|mssql|amqp|oracle)://[^\s"'\'']{10,}'
    mask: "[CONNSTR_REDACTED]"
    description: "Database and service connection strings."

  # ── Personal Identifiers ─────────────────────────────────
  name:
    pattern: r'\b(?:姓名|名字|称呼|name)\s*[:：]\s*[^\s\n]{2,20}\b'
    mask: "[NAME_REDACTED]"
    description: "Named person fields in Chinese or English context."

  date_of_birth:
    patterns:
      cn: r'(?:出生|生日|dob|出生日期)\s*[:：]?\s*(?:19|20)\d{2}[年\-/.](?:0[1-9]|1[0-2])[月\-/.]\d{1,2}'
      generic_date: r'\b(?:19|20)\d{2}[年\-/.](?:0[1-9]|1[0-2])[月\-/.]\d{1,2}\b'
    mask: "[DOB_REDACTED]"
    description: "Dates of birth in various formats."

  gender:
    pattern: r'\b(?:性别|sex|gender)\s*[:：]\s*(?:男|女|male|female|M|F)\b'
    mask: "[GENDER_REDACTED]"

  # ── Healthcare ───────────────────────────────────────────
  medical_record:
    pattern: r'\b(?:病历号|住院号|门诊号|MRN|病案号)\s*[:：]\s*\S{4,20}\b'
    mask: "[MEDICAL_REDACTED]"

  insurance:
    patterns:
      cn: r'\b[AMZ]\d{8,17}\b'
      us: r'\b\d{3}-\d{2}-\d{4}\b'
    mask: "[INSURANCE_REDACTED]"
    description: "Chinese social insurance card number or US health insurance IDs."

  # ── Vehicle ──────────────────────────────────────────────
  vin:
    pattern: r'\b[A-HJ-NPR-Z0-9]{13}\b'
    mask: "[VIN_REDACTED]"
    description: "Vehicle Identification Number. 17 chars, excludes I, O, Q."

  plate_cn:
    pattern: r'\b[一-龥][A-Z]\w{5}\b'
    mask: "[CN_PLATE_REDACTED]"
    description: "Chinese license plate: 1 Chinese char + letter + 5 alphanum."

  # ── Financial (China-specific) ───────────────────────────
  cn_bank_card:
    pattern: r'\b[62][0-9]{14,18}\b'
    mask: "[CN_BANK_CARD_REDACTED]"
    description: "Chinese UnionPay bank cards start with 62, 16-19 digits."
    validation: "Luhn algorithm"

  cn_fund_account:
    pattern: r'\b\d{14}\b'
    mask: "[CN_FUND_REDACTED]"
    description: "Chinese securities fund account number (14 digits)."

  # ── Education ────────────────────────────────────────────
  student_id:
    pattern: r'\b(?:学号|student[_\-]?id)\s*[:：]\s*\S{6,20}\b'
    mask: "[STUDENT_ID_REDACTED]"

  employee_id:
    pattern: r'\b(?:工号|employee[_\-]?id|emp[_\-]?id)\s*[:：]\s*\S{4,20}\b'
    mask: "[EMPLOYEE_ID_REDACTED]"

  # ── Geographic ───────────────────────────────────────────
  gps_coordinates:
    pattern: r'(?:lat|latitude|经度|纬度)\s*[:：]?\s*-?\d{1,3}(?:\.\d{6,})?\s*,\s*(?:lon|longitude|经度|纬度)\s*[:：]?\s*-?\d{1,3}(?:\.\d{6,})?'
    mask: "[GPS_REDACTED]"
    description: "GPS coordinates with 6+ decimal places (~0.1m precision)."

  # ── Exclusion Patterns (NOT PII) ─────────────────────────
  # These patterns should be excluded to reduce false positives:
  exclusion_patterns:
    version_numbers: r'\bv?\d+\.\d+\.\d+\b'
    uuids: r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b'
    hex_colors: r'#[0-9a-fA-F]{3,8}\b'
    iso_dates: r'\b\d{4}-\d{2}-\d{2}\b'
    product_skus: r'\b(?:SKU|产品编号|型号)\s*[:：]\s*[A-Z0-9\-]{3,20}\b'
```

#### Masking Tokens

Each PII category has a descriptive mask token that preserves cache semantics (same pattern → same mask → same cache key):

| Category | Mask Token | Example |
|----------|-----------|---------|
| Email | `[EMAIL_REDACTED]` | `user@domain.com` → `[EMAIL_REDACTED]` |
| Phone (CN mobile) | `[PHONE_REDACTED]` | `13812345678` → `[PHONE_REDACTED]` |
| CN ID card | `[CN_ID_REDACTED]` | `11010119900101123X` → `[CN_ID_REDACTED]` |
| Credit card | `[CC_REDACTED]` | `4111-1111-1111-1111` → `[CC_REDACTED]` |
| IP address | `[IP_REDACTED]` | `192.168.1.1` → `[IP_REDACTED]` |
| URL | `[URL_REDACTED]` | `https://example.com/path` → `[URL_REDACTED]` |
| Address (CN) | `[ADDRESS_REDACTED]` | `北京市朝阳区建国路1号` → `[ADDRESS_REDACTED]` |
| Password/key | `[CREDENTIAL_REDACTED]` | `password=abc123` → `password=[CREDENTIAL_REDACTED]` |
| Bank card | `[CN_BANK_CARD_REDACTED]` | `6217001234567890` → `[CN_BANK_CARD_REDACTED]` |
| Passport | `[PASSPORT_REDACTED]` | `G12345678` → `[PASSPORT_REDACTED]` |
| VIN | `[VIN_REDACTED]` | `LSVAU2A39GN123456` → `[VIN_REDACTED]` |
| GPS | `[GPS_REDACTED]` | `39.9042,116.4074` → `[GPS_REDACTED]` |

#### Processing Pipeline

```
Raw text
  │
  ▼
1. Exclusion pass — remove UUIDs, version numbers, hex colors, ISO dates, SKUs
  │     (reduce false positives)
  ▼
2. Named-field pass — match "key: value" patterns (email, phone, name, dob)
  │     (higher precision, context-aware)
  ▼
3. Standalone pattern pass — regex on raw text (ID cards, credit cards, IPs)
  │     (catches PII not preceded by a label)
  ▼
4. Apply mask tokens — replace each match with category-specific token
  │
  ▼
Sanitized text → proceed to pipeline
```

- **sanitize** (default): Replace matched patterns with mask tokens above
- **reject**: Return 400 error with PII detected message, listing detected categories
- **hash**: Replace with `SHA256(mask_token + original_value)` — preserves uniqueness for cache matching without exposing raw data

#### Performance Characteristics

- Regex compilation: all patterns compiled once at startup, stored in `re.Pattern` objects
- Named-field pass (step 2) runs first — ~5-10 patterns, O(n) with small constant
- Standalone pass (step 3) runs second — ~20 patterns, batched via `re.sub` with `count=0`
- Thread pool: for prompts >10KB, runs in `concurrent.futures.ThreadPoolExecutor(max_workers=4)`
- Sampling: for prompts >50KB, only first 10KB and last 10KB are scanned
- Estimated overhead: <0.5ms for prompts <1KB, <5ms for prompts <10KB, <20ms for prompts <50KB

### Quota Management

Per-API-key budget and quota controls:

```yaml
quotas:
  daily_token_limit: 1000000
  monthly_cost_limit: 50.00
  rate_limit_rpm: 60      # requests per minute
  rate_limit_tpm: 100000  # tokens per minute
```

- Checked in auth middleware before pipeline execution
- Exceeded quota → 429 Too Many Requests with retry-after header
- **Soft budget alerts**: When usage reaches 80% of budget, a warning is logged and pushed to the control panel (and optionally to email/webhook). This gives admins time to adjust budgets before service is cut off.
- Near-limit warning → logged and pushed to control panel
- Configurable thresholds per API key, managed via control panel

## Configuration

### Priority Order (highest to lowest)

1. **Environment variables** — always win
2. **YAML config file** — `config.yaml`
3. **Defaults** — hardcoded fallbacks in `config.py`

```bash
# Env var overrides
export AI_GATEWAY_CONFIG_PATH=/etc/aigateway/config.yaml
export AI_GATEWAY_LOG_LEVEL=debug
export AI_GATEWAY_REDIS_URL=redis://redis:6379/0
```

### YAML Config Example

```yaml
# config.yaml
server:
  host: 0.0.0.0
  port: 8000
  workers: 4

auth:
  api_keys:
    - key: sk-dev-xxx
      user_id: dev-user
      quotas:
        daily_tokens: 1000000
        monthly_cost: 50.00

plugins:
  - name: prompt_compress
    enabled: true
    depends_on: []
    config:
      pii_strategy: sanitize
      pii_patterns:
        phone: r'\b\d{10,}\b'
        email: r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
  - name: prompt_cache
    enabled: true
    depends_on: []
    config:
      ttl: 3600
      backend: redis
  - name: semantic_cache
    enabled: true
    depends_on: [prompt_compress]
    config:
      threshold: 0.95
      ttl: 86400
      backend: qdrant
  - name: conv_compressor
    enabled: false
    depends_on: []
    config:
      max_history: 10
      summary_interval: 5
  - name: rag_retriever
    enabled: false
    depends_on: []
    config:
      top_k: 20
      rerank_top_k: 3
      document_loader: auto  # auto-detect from file extension
      text_splitter:
        chunk_size: 512
        chunk_overlap: 64
  - name: model_router
    enabled: true
    depends_on: []
    config:
      strategy: quality  # cost | speed | quality
      fallback:
        - model: gpt-4o
          provider: openai
        - model: claude-3-5-sonnet
          provider: anthropic
      retries: 3
      retry_delay_ms: 1000
  - name: mcp_optimizer
    enabled: false
    depends_on: []
    config:
      max_result_size_kb: 10
      redis_ttl_seconds: 3600
  - name: response_formatter
    enabled: false
    depends_on: []
    config:
      format: json  # json | text
      max_tokens: 200

providers:
  openai:
    api_key: ${OPENAI_API_KEY}
  anthropic:
    api_key: ${ANTHROPIC_API_KEY}
  gemini:
    api_key: ${GEMINI_API_KEY}
  bedrock:
    region: us-east-1
  ollama:
    base_url: http://ollama:11434

embedding:
  backend: sentence_transformers  # sentence_transformers | openai
  model: all-MiniLM-L6-v2
  openai_model: text-embedding-3-small

observability:
  prometheus_enabled: true
  opentelemetry_enabled: true
  otel_service_name: ai-gateway
  otel_sample_rate: 0.1       # 10% of requests sampled (0.0 = off, 1.0 = all)
  log_format: json              # json | text
  log_level: info
```

## Performance Optimizations

1. **Full async I/O** — All plugin operations (Redis, Qdrant, LLM) use async clients
2. **Connection pooling** — Reuse connections to all downstream services (LiteLLM handles this)
3. **Zero-copy cache hits** — Cached responses stored as raw JSON bytes in Redis, returned directly
4. **Streaming support** — SSE streaming for chunked LLM responses
5. **Batch embeddings** — Coalesce multiple embedding requests into single batch calls
6. **Memory-resident templates** — Common prompt templates pre-loaded on startup
7. **Structured logging** — `structlog` with JSON output, includes `trace_id`, `request_id`, `user_id`
8. **Distributed tracing** — OpenTelemetry trace ID injected at entry, propagated through all plugins and downstream LLM calls

### Multi-Level Cache Strategy

Three-tier cache with progressive fallback:

```
Request → L1 (In-Process LRUCache) → L2 (Redis KV) → L3 (Qdrant Vector) → LLM
```

| Tier | Backend | Size | Latency | Use Case |
|------|---------|------|---------|----------|
| L1 | `cachetools.LRUCache` (process memory) | ~1000 entries | <1ms | Hottest requests/responses, same-process |
| L2 | Redis (bytes) | ~100K entries | <5ms | Distributed cache, shared across gateway instances |
| L3 | Qdrant (vector similarity) | Unlimited | <50ms | Semantic matching, cross-instance |

- L1 is per-process, fastest, no network overhead. Evicts via LRU when full.
- L2 is shared across all gateway instances in a cluster.
- L3 is the broadest — matches semantically similar queries.
- Cache key for L1/L2: SHA-256 of (normalized_prompt + model + params).
- Cache key for L3: embedding vector of normalized prompt.

### Circuit Breaker

Each downstream LLM provider has an independent circuit breaker (via `pybreaker`):

```yaml
circuit_breaker:
  openai:
    failure_threshold: 5      # Open after 5 consecutive failures
    recovery_timeout: 60      # Try half-open after 60s
    expected_exception: litellm.BadRequestError
  anthropic:
    failure_threshold: 3
    recovery_timeout: 120
```

- **CLOSED** → normal operation. After `failure_threshold` failures, transition to OPEN.
- **OPEN** → reject all requests to this provider immediately, fast-fail with fallback trigger.
- **HALF-OPEN** → after `recovery_timeout`, allow one probe request. If it succeeds, CLOSE; if it fails, OPEN again.
- Prevents cascade failures when a provider is degraded — gateway connections don't pile up waiting for timeouts.

## Error Handling & Fallback

### Circuit Breaker States

See `circuit_breaker` config above. Implemented per-provider in `litellm_bridge.py` using `pybreaker.CircuitBreaker`. The breaker wraps the LiteLLM completion call.

### Model Fallback (via LiteLLM)

LiteLLM Router supports automatic fallback chains:

```yaml
providers:
  openai:
    model_grouper:
      - models: [gpt-4o, gpt-4o-mini]
        fallback_models: [claude-3-5-sonnet]
    num_retries: 3
    retry_after: 1000
```

When the primary model fails (timeout, rate limit, 5xx), LiteLLM automatically retries with the next model in the fallback chain.

### Plugin Failure Isolation

Each plugin wraps its logic in try/except:
- **Cache plugins** (prompt_cache, semantic_cache): If the cache backend is down, skip cache and proceed to LLM. Log warning.
- **Compression plugins**: If compression fails, pass the original prompt through. Log warning.
- **RAG retriever**: If vector DB is unreachable, skip RAG and proceed to LLM. Log warning.
- **Non-critical plugins are fail-open** — the request always reaches the LLM if possible.

## Observability Correlation

Ensures logs, metrics, and traces share a unified `trace_id`:

- **Log → Metric**: Every Prometheus metric exported with `trace_id` label when available. Grafana panels can drill from a latency spike directly to the trace.
- **Metric → Trace**: Clicking a high-latency data point in Grafana opens the OpenTelemetry trace view filtered by that `trace_id`.
- **Trace → Log**: The trace detail page includes a log panel showing all `structlog` JSON entries for that trace.
- This creates a seamless path: **alert (metric) → investigate (trace) → diagnose (log)**.

## Error Handling & Fallback

### Model Fallback (via LiteLLM)

LiteLLM Router supports automatic fallback chains:

```yaml
providers:
  openai:
    model_grouper:
      - models: [gpt-4o, gpt-4o-mini]
        fallback_models: [claude-3-5-sonnet]
    num_retries: 3
    retry_after: 1000
```

When the primary model fails (timeout, rate limit, 5xx), LiteLLM automatically retries with the next model in the fallback chain.

### Plugin Failure Isolation

Each plugin wraps its logic in try/except:
- **Cache plugins** (prompt_cache, semantic_cache): If the cache backend is down, skip cache and proceed to LLM. Log warning.
- **Compression plugins**: If compression fails, pass the original prompt through. Log warning.
- **RAG retriever**: If vector DB is unreachable, skip RAG and proceed to LLM. Log warning.
- **Non-critical plugins are fail-open** — the request always reaches the LLM if possible.

## RAG Configuration

RAG retriever supports configurable document processing:

- **Document Loaders**: Auto-detect from file extension, or explicitly specify (`pdf`, `txt`, `csv`, `json`, `markdown`)
- **Text Splitters**: Configurable chunk size, overlap, and strategy (character, sentence, token)
- **Qdrant Collections**: Per-user collections supported; users upload files via control panel API
- **File Lifecycle**: When a file is updated, it is automatically reloaded, re-split, re-embedded, and the old vectors are deleted. When a file is deleted, its vectors are removed from Qdrant to prevent stale data.
- **Reranker**: `CrossEncoderReranker` (LlamaIndex) or `bge-reranker-base` (local)

## Dependencies

```
aigateway-core/requirements.txt:
  litellm>=1.40
  langchain>=0.2
  langchain-community>=0.2
  llama-index>=0.10
  redis[hiredis]>=5.0
  qdrant-client>=1.7
  sentence-transformers>=2.6
  prometheus-client>=0.20
  opentelemetry-api>=1.24
  opentelemetry-sdk>=1.24
  structlog>=24.0
  pydantic>=2.0
  pyyaml>=6.0
  watchdog>=3.0
  cachetools>=5.3
  pybreaker>=1.1

aigateway-api/requirements.txt:
  # inherits aigateway-core +
  fastapi>=0.110
  uvicorn[standard]>=0.29
  python-multipart>=0.0.6

aigateway-cli/requirements.txt:
  # inherits aigateway-core +
  rich>=13.0
  prompt-toolkit>=3.0

aigateway-ide/:
  # TypeScript extension, communicates with local gateway API

control-panel/:
  react>=18
  vite
  tailwindcss
  recharts
```

## Docker Compose Services

| Service | Image | Purpose |
|---|---|---|
| aigateway-api | self-built | Core gateway (FastAPI) |
| aigateway-cli | self-built | CLI tool (also runnable via `pip install`) |
| control-panel | self-built | React SPA + nginx |
| qdrant | qdrant/qdrant:latest | Vector database for semantic cache + RAG |
| redis | redis:7-alpine | KV cache + MCP large object storage |
| prometheus | prom/prometheus:latest | Metrics collection |
| grafana | grafana/grafana:latest | Visualization dashboard |

## IDE Extension Details

The IDE extension targets VS Code and Cursor:

- **Interception scope**: Intercepts `fetch`/`XMLHttpRequest` calls to known AI API domains (`api.openai.com`, `api.anthropic.com`, `generativelanguage.googleapis.com`)
- **Configuration**: Users configure the local gateway URL in extension settings (default: `http://localhost:8000/v1`)
- **Not zero-config by default**: Users must opt-in to enable interception, preventing accidental interference with non-AI traffic
- **Transparent**: All optimizations applied automatically; user sees no difference in UX

## Usage Examples

### Form 1: API Gateway (drop-in replacement)

```bash
# Set env vars, no code changes needed
export OPENAI_BASE_URL=http://localhost:8000/v1
export OPENAI_API_KEY=gateway-key

# Any existing OpenAI SDK app automatically goes through the pipeline
python my_app.py
```

### Form 2: CLI Tool

```bash
# Install globally
pip install aigateway-cli

# Interactive chat
aigateway chat

# Single request
aigateway run --prompt "Tell me about Kubernetes" --format json

# With session memory
aigateway chat --session my-project
```

### Form 3: IDE Extension

```
# Install from VS Code marketplace
# Configure gateway URL in extension settings
# Extension intercepts AI assistant requests automatically
```

### Form 4: Docker Compose (all-in-one)

```bash
docker compose up -d
# Gateway: http://localhost:8000
# Dashboard: http://localhost:3000
```

## Success Criteria

- [ ] API Gateway: Drop-in OpenAI API replacement (/v1/chat/completions, /v1/models, streaming)
- [ ] CLI: `aigateway chat` works out of the box with interactive session
- [ ] IDE: Extension intercepts AI assistant requests with configurable scope
- [ ] Prompt compression reduces input tokens by 30%+ on typical prompts
- [ ] Prompt cache hit rate > 50% on repeated requests
- [ ] Semantic cache hit rate > 30% on similar questions
- [ ] All plugins independently toggleable via config
- [ ] PipelineContext.extra uses namespaced keys to prevent cross-plugin collisions
- [ ] Cache hit short-circuits remaining plugins
- [ ] Streaming cache hits simulate SSE chunking (20ms/chunk delay)
- [ ] API Key auth with per-key quota management
- [ ] API Key changes synced across distributed instances via Redis Pub/Sub
- [ ] PII detection with configurable strategy (sanitize/reject/hash)
- [ ] PII detection covers 20+ categories: email, phone (CN/intl), CN ID card (15/18-digit), credit card, bank card, passport, driver license, IP address, URL, address (CN/US), password/credentials, connection strings, GPS coordinates, VIN, plate number, DOB, name, SSN (US), IBAN/SWIFT
- [ ] Named-field pass before standalone pattern pass (higher precision first)
- [ ] Exclusion patterns for UUIDs, version numbers, hex colors, ISO dates, SKUs (reduce false positives)
- [ ] Mask tokens are category-specific and deterministic (same input → same mask → cache-friendly)
- [ ] PII detection runs async (thread pool) and samples long prompts (>50KB)
- [ ] Estimated PII overhead: <0.5ms (<1KB), <5ms (<10KB), <20ms (<50KB)
- [ ] Config hot reload with atomic swap (no mid-request config corruption)
- [ ] Env var overrides YAML config
- [ ] Plugin dependency validation at startup
- [ ] Pipeline short-circuit on cache hits
- [ ] API Key auth with per-key quota management
- [ ] PII detection with configurable strategy (sanitize/reject/hash)
- [ ] Model fallback and retry via LiteLLM
- [ ] Fail-open plugin behavior (cache down → proceed to LLM)
- [ ] Prometheus metrics exposed at /metrics
- [ ] OpenTelemetry trace ID propagated through entire pipeline
- [ ] OTel configurable sampling rate (default 10%)
- [ ] Structured JSON logging with trace_id, request_id, user_id
- [ ] Config hot reload without restart
- [ ] Env var overrides YAML config
- [ ] Grafana dashboard shows cost, tokens, cache hit rate, latency
- [ ] docker-compose up brings up all services
- [ ] Control panel allows runtime plugin toggle, config, and quota management
- [ ] Soft budget alerts at 80% usage (warning, not rejection)
- [ ] RAG file lifecycle: upload → auto-index, update → re-index, delete → clean vectors
- [ ] Multi-level cache: L1 (LRUCache) → L2 (Redis) → L3 (Qdrant) progressive fallback
- [ ] Circuit breaker per provider: pybreaker with CLOSED/OPEN/HALF-OPEN states
- [ ] trace_id attached as Prometheus metric label for Grafana drill-down

## Future Considerations (v2+)

- **Sidecar deployment**: Design the core library to be embeddable as a sidecar container alongside business application Pods in K8s. Config sourced from ConfigMap/Consul instead of local file.
- **Plugin DAG execution**: Upgrade linear pipeline to DAG-based scheduling (networkx) enabling parallel execution of independent plugins.
- **Async task queue**: Offload RAG document processing and log ingestion to Celery workers for lower P99 latency.
- **Feature flag platform**: Integrate Unleash or similar for per-key plugin A/B testing and gradual rollout.

---

## Stage-Based Parallelism（阶段内并行调度方案）

> Status: v2+ 预案 — 当插件数量 > 10 或单插件耗时 > 50ms 时考虑实施
> 前置条件: MVP 线性管道稳定运行，性能瓶颈已通过 profiling 确认在插件调度层

### 设计理念

Stage-based parallelism 是介于纯线性管道和完整 DAG 调度之间的折中方案。核心思路：

1. 将插件按逻辑功能分为多个**阶段（Stage）**
2. **阶段间**严格串行（前一阶段全部完成后才进入下一阶段）
3. **阶段内**的插件无依赖关系，可以 `asyncio.gather()` 并行执行
4. 短路检查在**阶段边界**进行（而非每个插件之后）

这样保留了线性管道的可预测性和短路简洁性，同时在有并行机会的阶段获得延迟收益。

### 阶段划分

```
┌─────────────────────────────────────────────────────────────────┐
│  Stage 0: Pre-processing（预处理）                               │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │
│  │ PII Detector │  │ Prompt       │  │ Token        │          │
│  │              │  │ Compress     │  │ Counter      │          │
│  └──────────────┘  └──────────────┘  └──────────────┘          │
│  ↑ 这三个插件都只读 request.messages，写各自的 namespace         │
│  ↑ 可以并行执行                                                  │
├─────────────────────────────────────────────────────────────────┤
│  ★ 短路检查点: if ctx.should_stop → return                      │
├─────────────────────────────────────────────────────────────────┤
│  Stage 1: Cache Lookup（缓存查找）                               │
│  ┌──────────────┐  ┌──────────────┐                            │
│  │ Prompt Cache │  │ Semantic     │                            │
│  │ (L1 + L2)   │  │ Cache (L3)   │                            │
│  └──────────────┘  └──────────────┘                            │
│  ↑ 精确缓存和语义缓存可以并行查找                                 │
│  ↑ 任一命中即标记 should_stop                                    │
├─────────────────────────────────────────────────────────────────┤
│  ★ 短路检查点: if ctx.should_stop → return cached response      │
├─────────────────────────────────────────────────────────────────┤
│  Stage 2: Enrichment（增强）                                     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │
│  │ Conv         │  │ RAG          │  │ Context      │          │
│  │ Compressor   │  │ Retriever    │  │ Injector     │          │
│  └──────────────┘  └──────────────┘  └──────────────┘          │
│  ↑ 对话摘要、RAG检索、上下文注入互不依赖，可并行                   │
├─────────────────────────────────────────────────────────────────┤
│  ★ 短路检查点                                                    │
├─────────────────────────────────────────────────────────────────┤
│  Stage 3: Routing & Execution（路由与执行）                       │
│  ┌──────────────┐  →  ┌──────────────┐                         │
│  │ Model Router │     │ LiteLLM Call │                         │
│  └──────────────┘     └──────────────┘                         │
│  ↑ 路由决策 → LLM调用，天然串行                                   │
├─────────────────────────────────────────────────────────────────┤
│  Stage 4: Post-processing（后处理）                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │
│  │ Response     │  │ Cache        │  │ Metrics      │          │
│  │ Formatter    │  │ Backfill     │  │ Collector    │          │
│  └──────────────┘  └──────────────┘  └──────────────┘          │
│  ↑ 格式化响应、回填缓存、收集指标可并行                            │
└─────────────────────────────────────────────────────────────────┘
```

### 接口设计

```python
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from typing import List, Optional

@dataclass
class Stage:
    """一个执行阶段，包含可并行执行的插件列表。"""
    name: str
    plugins: List[Plugin]
    short_circuit_after: bool = True  # 阶段完成后是否检查短路
    fail_strategy: str = "continue"   # "continue" | "abort_stage" | "abort_pipeline"

class StagedPipelineEngine:
    """阶段内并行的管线引擎。

    与 PipelineEngine 接口兼容，可无缝替换。
    """

    def __init__(self, stages: List[Stage]) -> None:
        self._stages = stages

    async def execute(self, request: dict) -> dict:
        ctx = PipelineContext(request=request)
        ctx.should_stream = bool(request.get("stream", False))

        for stage in self._stages:
            # 阶段间短路检查
            if ctx.should_stop:
                self._mark_remaining_skipped(stage, ctx)
                continue

            # 阶段内并行执行
            results = await self._execute_stage(stage, ctx)

            # 阶段完成后短路检查
            if stage.short_circuit_after and ctx.should_stop:
                # 跳过后续所有阶段
                break

        return self._build_response(ctx)

    async def _execute_stage(self, stage: Stage, ctx: PipelineContext) -> None:
        """并行执行一个阶段内的所有插件。

        关键设计决策：
        1. 每个插件操作 ctx 的独立 namespace，不会冲突
        2. 全局字段（should_stop）使用 first-writer-wins 语义
        3. 单个插件失败不影响同阶段其他插件
        """
        if len(stage.plugins) == 1:
            # 单插件阶段，无需 gather 开销
            await self._run_plugin(stage.plugins[0], ctx)
            return

        # 多插件并行
        tasks = [
            asyncio.create_task(
                self._run_plugin(plugin, ctx),
                name=f"stage:{stage.name}:plugin:{plugin.name}",
            )
            for plugin in stage.plugins
        ]

        # 等待所有任务完成（即使某个失败，其余继续）
        await asyncio.gather(*tasks, return_exceptions=True)

        # 处理异常
        for task, plugin in zip(tasks, stage.plugins):
            if task.exception():
                exc = task.exception()
                logger.error(
                    "Stage %s plugin %s failed: %s",
                    stage.name, plugin.name, exc,
                )
                ctx.add_plugin_trace(plugin.name, 0, "failed")

    async def _run_plugin(self, plugin: Plugin, ctx: PipelineContext) -> None:
        """执行单个插件，包含计时和异常处理。"""
        start = time.monotonic()
        try:
            await plugin.execute(ctx)
            elapsed_ms = (time.monotonic() - start) * 1000
            ctx.add_plugin_trace(plugin.name, elapsed_ms, "success")
        except Exception as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            ctx.add_plugin_trace(plugin.name, elapsed_ms, "failed")
            raise
```

### 配置格式

```yaml
# config.yaml — 阶段式管线配置
pipeline:
  mode: staged  # "linear" (MVP) | "staged" (v2)

  stages:
    - name: pre_processing
      parallel: true
      short_circuit_after: true
      plugins:
        - pii_detector
        - prompt_compress
        - token_counter

    - name: cache_lookup
      parallel: true
      short_circuit_after: true
      plugins:
        - prompt_cache
        - semantic_cache

    - name: enrichment
      parallel: true
      short_circuit_after: false
      plugins:
        - conv_compressor
        - rag_retriever
        - context_injector

    - name: routing_execution
      parallel: false  # 串行：先路由后调用
      short_circuit_after: false
      plugins:
        - model_router
        - litellm_completion

    - name: post_processing
      parallel: true
      short_circuit_after: false
      plugins:
        - response_formatter
        - cache_backfill
        - metrics_collector
```

### PipelineContext 并发安全

阶段内并行要求对 `PipelineContext` 的并发访问做出约束：

```python
class PipelineContext:
    # === 只读字段（创建后不变） ===
    request: dict              # 原始请求，所有插件只读
    request_id: str
    trace_id: str
    user_id: Optional[str]

    # === 插件隔离字段（每个插件写自己的 namespace） ===
    # 使用 dict 的 key 隔离，不同 key 无竞争
    pii_detector: dict = {}       # 仅 PII 插件写
    prompt_cache: dict = {}       # 仅缓存插件写
    semantic_cache: dict = {}     # 仅语义缓存写
    model_router: dict = {}       # 仅路由插件写
    # ...

    # === 全局可变字段（需要原子操作） ===
    _should_stop: bool = False
    _stop_reason: str = ""
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def mark_stopped(self, reason: str) -> None:
        """线程安全的短路标记（first-writer-wins）。"""
        async with self._lock:
            if not self._should_stop:
                self._should_stop = True
                self._stop_reason = reason

    @property
    def should_stop(self) -> bool:
        return self._should_stop
```

**设计原则：**
- 插件只写自己 namespace 的字段 → 无锁
- `should_stop` 使用 `asyncio.Lock` + first-writer-wins → 避免后写覆盖
- `response` 字段由缓存插件通过 `mark_stopped()` 原子设置

### 短路语义

```
阶段内并行时的短路处理：

1. Stage 1 内 prompt_cache 和 semantic_cache 同时查询
2. 假设 prompt_cache 先命中 → 调用 ctx.mark_stopped("cache_hit=L2")
3. semantic_cache 的 embedding 计算可能还在进行中
4. 由于 gather() 会等所有任务完成，semantic_cache 会自然结束
   （它的结果会被忽略，因为 should_stop 已经为 True）
5. Stage 1 结束后检查 should_stop → True → 跳过后续 Stage

注意：不使用 Task.cancel()，避免取消带来的复杂性。
让已启动的任务自然完成，仅在阶段边界做短路判断。
代价是浪费少量计算，但换来了确定性和简洁性。
```

### 与 MVP 线性管道的兼容性

```python
class PipelineEngineFactory:
    """根据配置选择管线引擎实现。"""

    @staticmethod
    def create(config: dict, registry: PluginRegistry) -> PipelineEngine:
        mode = config.get("pipeline", {}).get("mode", "linear")

        if mode == "linear":
            # MVP 模式：拓扑排序 + 线性执行
            engine = LinearPipelineEngine(registry)
        elif mode == "staged":
            # v2 模式：阶段内并行
            stages = StageBuilder.build_from_config(
                config["pipeline"]["stages"],
                registry,
            )
            engine = StagedPipelineEngine(stages)
        else:
            raise ValueError(f"Unknown pipeline mode: {mode}")

        engine.initialize()
        return engine
```

升级路径：
1. 修改 `config.yaml` 中 `pipeline.mode` 从 `linear` → `staged`
2. 添加 `stages` 配置
3. 无需修改任何插件代码（接口不变）
4. 可随时回退到 `linear` 模式

### 性能预估

以当前插件为例，假设未来启用全部插件：

| 模式 | Stage 0 | Stage 1 | Stage 2 | Stage 3 | Stage 4 | 总计 |
|------|---------|---------|---------|---------|---------|------|
| 线性 | 5+2+1=8ms | 5+50=55ms | 30+80+5=115ms | 2+LLM | 3+5+1=9ms | ~187ms + LLM |
| 阶段并行 | max(5,2,1)=5ms | max(5,50)=50ms | max(30,80,5)=80ms | 2+LLM | max(3,5,1)=5ms | ~142ms + LLM |
| 节省 | — | — | — | — | — | **~45ms (24%)** |

当 RAG 检索（80ms）和对话摘要（30ms）同时启用时，Stage 2 的并行收益最为明显。

### 何时触发升级

建议在满足以下任一条件时从线性管道升级到阶段式并行：

1. **启用插件数 > 8 个**，且存在 2+ 个无依赖的独立插件
2. **P99 管线延迟 > 100ms**（不含 LLM 调用时间），profiling 确认瓶颈在插件串行等待
3. **RAG + Conv Compressor 同时启用**，Stage 2 串行延迟超过 100ms
4. **用户反馈首字节延迟（TTFB）不达标**，需要压缩管线开销

### 实施步骤

1. 将 `PipelineEngine` 重命名为 `LinearPipelineEngine`
2. 新建 `StagedPipelineEngine`，实现 `execute()` 接口
3. 添加 `PipelineEngineFactory`，根据 config 选择实现
4. 给 `PipelineContext` 的 `should_stop` 加 `asyncio.Lock`
5. 编写阶段配置解析器 `StageBuilder`
6. 添加 per-stage metrics（每阶段耗时、并行度）
7. 集成测试：验证阶段并行结果与线性模式一致（属性测试）
