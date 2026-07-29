# Code Review Report — ComfyUI GPU Pipeline (working-tree delta)

| Field | Value |
|---|---|
| Review date | 2026-07-29 |
| Branch | `feat/comfyui-gpu-pipeline` (uncommitted working-tree changes after `0a728f5`) |
| Reviewer | `/code-review` (background agent) + manual verification of top findings |
| Scope | Uncommitted diff in the working tree: `dispatcher.py`, `draft_generator.py`, `draft_generator_plugin.py`, `admin_routes.py`, `local_generation.py`, `integration_configs.py`, `registration.py`, control-panel chat/config, `model-manager.sh`, and the new test files |
| Outcome | **2 critical/bug, 2 bug, 1 edge, 2 informational — issues found** |
| Status | All findings are in the working tree, **uncommitted** (per workflow rule 0 — no auto-commit) |

> This report covers the *delta* on top of the earlier review at
> `docs/reviews/2026-07-29-comfyui-gpu-pipeline-review.md` (which reviewed the
> committed `cbf8bb1` branch). The findings below are against the subsequent
> uncommitted edits in the working tree.

---

## Summary

The new 503 "local backend unavailable" early-return in `dispatcher.py` is the
top finding: it bypasses the quota-reservation refund that every parallel
error path performs, so a user who hits a downed ComfyUI both gets a 503 and
**permanently loses the reserved token/cost quota** for a request that rendered
no service. Two more user-facing bugs follow: a false 503 for video +
`faithful_4k`, and silent dropping of the `faithful_4k` quality when
`backend='cloud'` is selected.

Findings are listed most-severe first. Top two were verified by reading the
actual source (line numbers shown), not just the diff.

---

## Critical / Bug Findings

### F1 — Quota reservation leak on the new 503 early-return (P1)

**Location:** `aigateway-core/src/aigateway_core/dispatch/dispatcher.py:788`
**Severity:** P1 · **Confidence:** verified · **Status:** confirmed against source

The new draft-confirm gate returns 503 immediately when
`draft_info.get("reason") == "local_backend_unavailable"`:

```python
# dispatcher.py:785-800
if ctx is not None:
    gen_opt = getattr(ctx, 'extra', {}) or {}
    draft_info = gen_opt.get("generation_optimization", {}).get("draft_generator", {})
    if draft_info.get("reason") == "local_backend_unavailable":
        return JSONResponse(status_code=503, content={...})
```

Quota is reserved **earlier** in the request lifecycle
(`dispatcher.py:703` sets `request.state._lua_quota_reserved = True`). Every
parallel error path refunds it before returning:

- `dispatcher.py:936` → `await self._release_quota_reservation(...)`
- `dispatcher.py:966` → `await self._release_quota_reservation(...)`
- `dispatcher.py:1328` → `await self._release_quota_reservation(...)`

The new 503 path does **not**. `_release_quota_reservation`
(`dispatcher.py:1434`) is a no-op only when `_lua_quota_reserved` is already
`False`, so adding the call is safe in all cases.

**Failure scenario:** A user sends `generation_options.backend='local'`
(or `quality='faithful_4k'`, which forces local) while ComfyUI is down. The
quota-check step has already reserved daily-token / monthly-cost / RPM / TPM
quota against the key and group. The new 503 returns without refunding, so the
reserved usage is **permanently counted** against the user's quota for a
request that produced no output. Repeated attempts can exhaust the daily token
budget via no-op 503s alone.

**Fix:** add `await self._release_quota_reservation(request, key_store, key_hash)`
before the `return JSONResponse(...)` at line 789, matching the pattern at 936/966.

---

### F2 — `install_qwen_image` over-requires disk space (P2, bug)

**Location:** `scripts/model-manager.sh:234`
**Severity:** P2 · **Confidence:** verified · **Status:** confirmed against source

```bash
# model-manager.sh:53-54
require_download_space() {
  local minimum_gb="${1:-80}"     # ← defaults to 80 GB

# model-manager.sh:234
  [[ "$all_installed" == "true" ]] || require_download_space        # ← no arg → 80 GB

# model-manager.sh:187 (the correct pattern, same function)
  [[ "$all_installed" == "true" ]] || require_download_space 40
```

The Qwen-Image model set downloads ~30 GB, but the call at line 234 omits the
argument and inherits the 80 GB default.

**Failure scenario:** Running `aigateway model install qwen-image` on a volume
with 50 GB free: `require_download_space` defaults to `minimum_gb=80`,
`(( 50*1024*1024 >= 80*1024*1024 ))` is false, so the script prints
`模型下载已拒绝：可用空间低于 80GB` and exits — even though the ~30 GB
download would fit comfortably.

**Fix:** `require_download_space 40` at line 234 (matching line 187).

---

### F3 — False 503 for video + `faithful_4k` (P2, bug)

**Location:** `aigateway-core/src/aigateway_core/pipelines/generation/draft/draft_generator.py:813`
**Severity:** P2 · **Confidence:** high · **Status:** confirmed by flow inspection

`check_local_dependencies` unconditionally appends the RealESRGAN upscale
model to `required` whenever `request.quality == 'faithful_4k'`, and raises
`comfyui_missing_dependencies` if it is absent. But the video confirm path
(`_confirm_video_draft` → `_do_video_generation`) never calls
`_build_faithful_upscale_workflow` — RealESRGAN is only used by the image
confirm path.

**Failure scenario:** A user with video models installed but no RealESRGAN
weight sends a video request (`pipeline_kind='generation:video'`) with
`quality='faithful_4k'`. `check_local_dependencies` rejects it as
`local_backend_unavailable` → 503, even though the video generation would have
succeeded without the upscale model. The UI likely does not prevent the
video+faithful_4k combination.

**Fix:** scope the upscale-model requirement to the image path only — e.g.
`if request.quality == 'faithful_4k' and media_type != 'video': required.append(...)`
(or gate by `pipeline_kind != 'generation:video'`).

---

### F4 — `faithful_4k` + `backend='cloud'` silently ignored (P2, bug)

**Location:** `aigateway-core/src/aigateway_core/pipelines/generation/draft/draft_generator_plugin.py:140`
**Severity:** P2 · **Confidence:** high · **Status:** confirmed by flow inspection

The plugin's `backend == 'cloud'` guard returns `applicable: False` **before**
any quality handling or `check_local_dependencies` runs. The cloud bridge has
no `faithful_4k` concept, so a cloud request with `quality='faithful_4k'`
proceeds as a standard-resolution cloud image.

**Failure scenario:** A user selects `quality='faithful_4k'` and
`backend='cloud'` (or `backend='auto'` with ComfyUI up so the local faithful_4k
check passes, then falls back to cloud). The request returns a normal-resolution
image while the UI indicated a 4K faithful upscale. No error surfaces; the
quality selection is silently dropped.

**Fix:** either return an explicit `applicable: False` with a `reason` that the
dispatcher surfaces as a 400 ("faithful_4k requires local backend"), or reject
the combination at validation time.

---

## Edge / Informational Findings

### F5 — `_faithful_upscale_resolution` can downscale (edge)

**Location:** `aigateway-core/src/aigateway_core/pipelines/generation/draft/draft_generator.py:1466`

`scale = max_edge / max(width, height)` has no `max(scale, 1.0)` guard. If a
source image's long edge ever exceeds `max_upscale_long_edge` (4096) — e.g. a
future `draft_resolution` bump or an external keyframe image — the
`_build_faithful_upscale_workflow` `ImageScale` node downscales below 4K while
still labeling the result `algorithm_used='comfyui:realesrgan:...'`.

Current draft previews are 1024×1024, so this is latent rather than active.
Clamp `scale = max(scale, 1.0)` to be safe.

---

### F6 — `probe_comfyui` disk scan runs on every poll even when HTTP failed (informational)

**Location:** `aigateway-api/src/aigateway_api/local_generation.py:242`

`shutil.disk_usage(models_path)` runs inside `asyncio.to_thread` on every
probe, including when the ComfyUI HTTP probe already failed. On a config where
`models_path` points to a slow or large network mount, every
`/admin/comfyui/status` and `/admin/generation-presets` poll pays the
`stat` cost on an unavailable server. Consider skipping the disk step when the
HTTP probe already failed, or caching it.

---

### F7 — `get_generation_presets` runs a full ComfyUI probe on every list, no caching (informational)

**Location:** `aigateway-api/src/aigateway_api/admin_routes.py:94`

`get_generation_presets` calls `probe_comfyui` (3 concurrent HTTP GETs to
`/system_stats`, `/object_info`, `/queue` plus the disk scan) on every list
call with no caching. `Config.tsx`'s `presetsQuery` refetches on window focus,
and `comfyQuery` already polls every 30 s — so the probe work is duplicated and
ComfyUI takes avoidable load on every window focus. Consider a short TTL cache
on the probe result, or de-dup against `comfyQuery`.

---

## Recommended fix order

1. **F1** (one-line correctness) — add the quota refund before the 503 return.
2. **F2** (one-line correctness) — pass `40` to `require_download_space`.
3. **F3** — scope the upscale-model requirement to the image path.
4. **F4** — surface an explicit error for `faithful_4k` + cloud.
5. F5–F7 — harden when convenient.

Per workflow rule 0, nothing has been committed; these remain in the working
tree pending review.
