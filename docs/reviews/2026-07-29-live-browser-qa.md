# Live Browser QA — Control Panel Issues (2026-07-29)

| Field | Value |
|---|---|
| Date | 2026-07-29 |
| Method | Real browser via Playwright MCP against live gateway (`:8000`) + panel (`:3000`) |
| Logged in as | `admin` (console login) |
| Branch | `feat/comfyui-gpu-pipeline` (uncommitted working tree) |
| Status | **2 confirmed user-facing bugs — root causes found, fixes identified** |

> User reported: (1) "总成本都不对" (total cost wrong), (2) chat returns
> "操作失败:draft_failed". Both reproduced live. Root causes below.

---

## Bug 1 — Overview "总成本" shows $0 while real spend is $3659

### Reproduction (live)

Navigated to `http://localhost:3000/` (Overview page). The five stat cards render:

| 卡片 | 显示值 | 真实值（账本 `request_cost_ledger`） |
|---|---|---|
| 总请求数 | 48 | — |
| 平均延迟 | 2022 ms | — |
| **总成本** | **$0.0000** ← 错 | **$3659.02**（今日 $3071.40） |
| 缓存命中率 | 0 % | — |
| Token 节省 | 0 | — |

"成本分布 by 用户" 区域显示"暂无用户成本数据"。

### Root cause

`control-panel/src/pages/Overview.tsx:21` computes total cost from a
Prometheus metric that **does not exist as a populated series**:

```ts
const totalCost = sumByMetric('gateway_cost_by_model_total')
```

Raw `/metrics` (verified live) emits only the HELP/TYPE header lines for this
counter — **zero actual samples** — because nothing ever calls
`MetricsCollector.record_cost()` with a real cost on the path the Overview
reads, OR the counter is being incremented on a different collector instance
than `/metrics` scrapes:

```
# HELP gateway_cost_by_model_total Total cost by model
# TYPE gateway_cost_by_model_total counter
        ← no sample lines here at all
# HELP gateway_cost_by_user_total Total cost by user
# TYPE gateway_cost_by_user_total counter
        ← no sample lines here either
```

Meanwhile `gateway_cost_total` (gauge) is also `0.0` despite the SQLite ledger
holding $3659.02 across 233 `ok` rows (verified directly in
`data/auth.db:request_cost_ledger`).

So `record_cost` is **defined** (`shared/metrics.py:354`) and **called**
(`dispatcher.py:1061, 1357` + `streaming/metrics_wrapper.py:48`), but the
counter/gauge values never make it into the Prometheus registry that `/metrics`
serves. The ledger write (`key_store.record_request_cost`, `dispatcher.py:1070`)
**does** work — which is why the Costs page (next section) is correct.

### Contrast: Costs page is correct

`http://localhost:3000/costs` renders from a different source and is accurate:

| 卡片 | 显示值 | 账本核对 |
|---|---|---|
| 总成本 (近7天) | $3453.04 | ✓ matches 7-day ledger sum |
| 平均单次成本 | $11.745 | ✓ |
| 模型数 | 5 | ✓ |
| 成本分布 by 用户组 | grp-admin-team $2287.48 / unknown $1154.94 / grp-default $10.62 | ✓ |

Costs.tsx reads the SQLite ledger aggregates + `gateway_cost_by_group_total`
(which **does** have samples — only the by-model and total gauges are empty).

### Fix direction

The metrics pipeline (`record_cost` → `_cost_by_model_counter` /
`_cost_total_gauge`) is not landing in the registry `/metrics` scrapes, even
though the same call site writes to the SQLite ledger fine. Two candidates to
investigate:

1. `dispatcher.py` `record_cost` runs under a conditional (`if metrics_collector
   and tt > 0 and final_cost > 0`) — confirm `metrics_collector` is the same
   singleton `routes.py` reads via `app.state.metrics_collector`, and that
   `final_cost > 0` actually holds on these requests (the ledger rows show
   non-zero cost, so the value exists; the branch should fire).
2. The `_cost_total_gauge.inc(cost_usd)` at `metrics.py:374` uses
   `Gauge.inc(amount)` — verify prometheus_client Gauge supports `inc(amount)`
   (it does) and that the gauge isn't being re-created on hot-reload losing
   state.

Lowest-effort reliable fix: make Overview read cost from the **same ledger
aggregate** the Costs page already uses successfully, instead of the empty
`gateway_cost_by_model_total` metric. That removes the dependency on the
broken metric entirely.

---

## Bug 2 — Chat image generation returns "操作失败:draft_failed"

### Reproduction (live)

On `http://localhost:3000/chat`, created a new conversation, sent
"画一只橘猫坐在窗台上晒太阳" with default composer settings
(`backend=auto`, `quality=standard`). The message renders:

```
🎨 图片 · draft  操作失败:draft_failed
```

Browser console shows the underlying 410:

```
GET /aigateway/admin/draft/18b2a66cee074445bbb8c77d6c339347/preview → 410 Gone
```

### Root cause

The draft's `meta.json`
(`data/drafts/sess-1785323948460-hlz24e/18b2a66c.../meta.json`) shows:

```json
{
  "status": "failed",
  "stage": "failed",
  "error": "comfyui_generation_failed",
  "generation_params": {
    "checkpoint": "qwen_image_fp8_e4m3fn.safetensors",
    "preset_id": "qwen-image",
    "quality": "standard"
  }
}
```

The local ComfyUI backend **failed to execute the workflow**. The 410
(`admin_routes.py:3046` maps `status in {failed, cancelled}` →
`draft_failed`) is just the frontend surfacing that backend failure.

### Why ComfyUI failed — triton can't find a C compiler

`docker logs aigateway-comfyui-1` shows the Qwen-Image text encoder (a Llama
architecture) crashing during RoPE precompute because **triton JIT cannot
compile its kernel** — no C compiler in the image:

```
File "/opt/ComfyUI/comfy/text_encoders/llama.py", line 430, in precompute_freqs_cis
    freqs = (inv_freq_expanded.float() @ position_ids_expanded.float()).transpose(1, 2)
  ...
  File "/usr/local/lib/python3.12/site-packages/triton/runtime/build.py", line 32, in _build
    raise RuntimeError(
RuntimeError: Failed to find C compiler. Please specify via CC environment variable
or set triton.knobs.build.impl.
```

Verified directly in the running container:

```
$ docker exec aigateway-comfyui-1 sh -c 'which gcc cc; gcc --version'
gcc: not found
CC=unset
```

### The Dockerfile bug

`aigateway-api/Dockerfile` `comfyui` target (lines 241–247) installs only:

```dockerfile
RUN ... apt-get install -y --no-install-recommends \
      git libegl1 libgbm1 libx11-6 libxdamage1 libxext6 libxfixes3 libxrandr2
```

**Missing: `build-essential`** (which provides `gcc`).

This is the exact gotcha already documented in `CLAUDE.md` for the **gateway**
Dockerfile:

> **Dockerfile must ship `build-essential`** — torch 2.13 + CUDA JIT-compiles
> kernels via triton, which needs a C compiler. Without `gcc`, the first
> embedding forward pass synchronously blocks / fails. `build-essential` in the
> apt layer is the fix (added 2026-07-09). Don't remove it to shrink the image.

The gateway target was fixed on 2026-07-09, but the **`comfyui` target was
missed** — it derives from `gateway-cuda-common` (torch+CUDA) without adding
`build-essential`, so triton JIT fails on the first Llama-based text encoder
forward pass (Qwen-Image → `precompute_freqs_cis`).

### Fix

Add `build-essential` to the comfyui target's apt install
(`aigateway-api/Dockerfile:246-247`):

```diff
 RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
     --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
     apt-get update && apt-get install -y --no-install-recommends \
-      git libegl1 libgbm1 libx11-6 libxdamage1 libxext6 libxfixes3 libxrandr2
+      build-essential git libegl1 libgbm1 libx11-6 libxdamage1 libxext6 libxfixes3 libxrandr2
```

Then rebuild + verify (per workflow rule 1):

```bash
sudo DOCKER_BUILDKIT=1 docker compose up -d --build comfyui
sudo docker exec aigateway-comfyui-1 sh -c 'which gcc'   # → /usr/bin/gcc
# re-send a chat image prompt; draft should reach status=ready (200)
```

### Secondary observation — stale 404 poll

The chat page also carries a **stale draft id** (`8b5121e9...`) from a prior
session that keeps polling `/admin/draft/{id}/preview` and `/result`,
returning 404 every poll (visible in console on every `/chat` load). This is
noise, not the cause of `draft_failed`, but worth cleaning up: the poller
should stop on the first 404 (`draft_not_found` / `expired`) rather than
retrying. `chatRuntime.ts:54` already handles `not_found`/`expired` — confirm
the 404 response body carries one of those codes so the early-exit fires.

---

## Summary of fixes

| # | Bug | Fix | Effort |
|---|---|---|---|
| 1 | Overview 总成本 = $0 (empty `gateway_cost_by_model_total` metric) | Make Overview read from the ledger aggregate the Costs page already uses; **or** fix `record_cost` so the counter/gauge lands in the scraped registry | medium |
| 2 | chat `draft_failed` (ComfyUI triton: no C compiler) | Add `build-essential` to the `comfyui` Dockerfile target, rebuild `comfyui` image | one line + rebuild |

Per workflow rule 0, nothing has been committed; these remain in the working
tree pending review.
