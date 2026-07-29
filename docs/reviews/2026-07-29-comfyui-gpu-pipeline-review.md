# Code Review Report — ComfyUI GPU Pipeline Branch

| Field | Value |
|---|---|
| Review date | 2026-07-29 |
| Branch | `main` (working tree from `feat/comfyui-gpu-pipeline`) |
| Base | `a9944e3` |
| HEAD | `cbf8bb1` |
| Diff size | 254 files, +6613 / −4387 |
| Unit tests | 1738 passing |
| Reviewer | `/code-review high` (agent) + manual verification |
| Outcome | **2 critical, 7 informational — issues found** |
| PR Quality Score | 6.0 / 10 |

---

## Scope Check: CLEAN (informational)

The bulk of the 254-file diff is a mechanical type-annotation refactor
(`Optional[X] → X | None`, `Dict → dict`, PEP 695 generics; 415 `Optional[`
lines removed). Python 3.12+ is required, so this is valid. The substantive
risk is concentrated in:

- the rewritten draft generator (ComfyUI-required path),
- the new draft-owner plumbing,
- console-chat auth,
- Dockerfile.

All 9 findings below were **verified by reading the actual code**, not just
the diff. Line numbers reference the post-diff source.

---

## Critical Findings

### C1 — Video confirm always fails by default

**Location:** `aigateway-core/src/aigateway_core/pipelines/generation/draft/draft_generator.py:1405`
**Severity:** P1 · **Confidence:** 9/10 · **Status:** verified

`ComfyUIConfig.video_enabled` defaults to `False`
(`aigateway-core/src/aigateway_core/shared/integration_configs.py:79`).

The video **preview** path `_generate_video_previews_with_comfyui` (line ~1440)
delegates to `_generate_image_preview_with_comfyui` — it generates an SDXL
**image keyframe** with **no** `video_enabled` check, so the preview succeeds
and the UI renders a confirm button.

The video **confirm** path `_generate_video_with_comfyui` (line 1405) does:

```python
if not self._comfyui_config.video_enabled:
    raise DraftWorkflowError("comfyui_video_not_enabled")
```

→ `_mark_draft_confirmation_failed` rolls the draft back to `pending` → the
admin route maps the failure to `400 draft_confirm_failed`.

**Failure scenario:** User generates a video draft preview (succeeds, returns
a keyframe), clicks "confirm generate video", and receives `400
draft_confirm_failed`. Every video confirm fails until an operator sets
`video_enabled: true` in `config.yaml` — but the UI offers the button
unconditionally.

**Fix options:**
1. Default `video_enabled=True` if local ComfyUI is the intended video backend.
2. Gate the video confirm button in the frontend on a `video_enabled`
   capability flag reported by `/admin/capabilities`.
3. At minimum, surface a distinguishable `video_not_enabled` error so the UI
   can hide/disable the button instead of showing a generic
   `draft_confirm_failed`.

---

### C2 — `delete_session` rejects ownerless legacy drafts; `_assert_draft_owner` allows them

**Location:** `aigateway-core/src/aigateway_core/pipelines/generation/draft/draft_generator.py:2399`
and `aigateway-api/src/aigateway_api/admin_routes.py:2779`
**Severity:** P1 · **Confidence:** 9/10 · **Status:** verified

The two owner-check policies contradict each other.

Per-draft helper `_assert_draft_owner` (`admin_routes.py:2779`):

```python
if not draft_user_id and not draft_group_id:
    # Compatibility for drafts created before owner metadata was persisted.
    return
```

Ownerless drafts are **allowed** for view / confirm / reject.

`delete_session` (`draft_generator.py:2399`):

```python
if not draft_user_id and not draft_group_id:
    raise DraftWorkflowError("draft_session_owner_unknown")
```

Ownerless drafts are **rejected** for session deletion.

**Failure scenario:** A session contains a draft created before owner metadata
was persisted (`draft.user_id` and `draft.group_id` both `None`). The operator
can view, confirm, and reject that individual draft, but `DELETE
/admin/drafts/session/{id}` raises `draft_session_owner_unknown` → 403. The
session directory and Redis keys leak until the 24h `DraftSessionCleaner`
sweeps.

**Fix:** Pick one policy and apply it in both places. The natural choice is to
make `delete_session` treat ownerless drafts the same way
`_assert_draft_owner` does — skip the owner check for them (or allow deletion
when the caller is an admin principal) — instead of raising.

---

## Informational Findings

### I1 — Dead `VideoSubmitResult` branch in confirm route

**Location:** `aigateway-api/src/aigateway_api/admin_routes.py:2986`
**Severity:** P2 · **Confidence:** 9/10 · **Status:** verified

`DraftGeneratorStrategy.confirm_draft` is annotated `-> UpscaleResult`
(`draft_generator.py:463`) and both branches return `UpscaleResult`:

- image: `_upscale_with_comfyui` → bytes → `UpscaleResult`
- video: `_generate_video_with_comfyui` → bytes → `UpscaleResult`

The admin route still does (line 2986-2999):

```python
from aigateway_core.pipelines.generation._common.models import VideoSubmitResult
if isinstance(result, VideoSubmitResult):
    ...
    "video_id": result.video_id,
```

This branch is unreachable. The bridge `_do_video_generation`,
`GET /v1/videos/{id}`, `/admin/console/videos/{id}`, and `pollVideoUntilTerminal`
are orphaned. Future maintainers will assume video still flows through Agnes
`/videos` polling and mis-wire changes.

**Fix:** Delete the `VideoSubmitResult` branch and the `video_id` field from
the confirm response. Either remove the orphaned video-polling endpoints and
bridge methods, or clearly mark them as dead.

---

### I2 — `_ensure_storage_capacity` walks the models dir on every request

**Location:** `aigateway-core/src/aigateway_core/pipelines/generation/draft/draft_generator.py:821`
**Severity:** P2 · **Confidence:** 8/10 · **Status:** verified

`_directory_size` (line ~786) does a full `os.walk` + `os.path.getsize` on
every file. `_ensure_storage_capacity` is called at lines 1367, 1409, 1456
(previews) and inside confirm.

ComfyUI `models_path` typically holds tens of GB across thousands of files.
With `max_concurrency=1` (default), this walk runs per request, adding seconds
of disk I/O before ComfyUI is called and competing with the generation job for
disk bandwidth.

**Fix:** Cache the size check with a short TTL (30-60s), or move it to a
background janitor that sets a flag instead of walking on every request.

---

### I3 — Redis load+store held inside the GPU semaphore

**Location:** `aigateway-core/src/aigateway_core/pipelines/generation/draft/draft_generator.py:1374`
**Severity:** P2 · **Confidence:** 8/10 · **Status:** verified

`_generate_image_preview_with_comfyui` acquires `self._comfyui_semaphore`
(line 1368, default size 1), then calls `_record_comfy_job` (1374), which does
`_load_draft` (Redis GET + file read) and `_store_draft` (Redis SET + file
write) **before** releasing the semaphore to `_poll_result`.

The semaphore gates GPU concurrency but is held during network/disk I/O it
doesn't need to guard. With `max_concurrency=1` the entire Redis round-trip
blocks the next draft from starting its workflow submission.

**Fix:** Move `_record_comfy_job` outside the semaphore (record before
acquiring, or after releasing), so the semaphore only gates the actual
ComfyUI submit + poll.

---

### I4 — `delete_session` aborts on a single stray-named directory entry

**Location:** `aigateway-core/src/aigateway_core/pipelines/generation/draft/draft_generator.py:2378`
**Severity:** P3 · **Confidence:** 8/10 · **Status:** verified

Line 2378:

```python
for draft_id in draft_ids:
    self._safe_path_component(draft_id, "draft_id")
```

This validates every entry from `os.listdir(session_dir)` (including entries
not in the Redis index) against `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`.

**Failure scenario:** A leftover `.tmp` dir, a name with a space, or a
non-ASCII name from an older deploy fails the regex → raises
`invalid_draft_id` → admin `delete_session_drafts` catches it as a generic
`Exception` → `500 internal_error`. One bad entry blocks deletion of the
entire session.

**Fix:** Skip-and-log invalid entries rather than aborting, or validate only
entries from the Redis index (the trusted source) and `rmtree` stray dirs
directly.

---

### I5 — `chat_session_id` Pydantic pattern looser than `_SAFE_PATH_COMPONENT`

**Location:** `aigateway-api/src/aigateway_api/openai_compat.py:71`
**Severity:** P3 · **Confidence:** 9/10 · **Status:** verified

Pydantic field (`openai_compat.py:71`):

```python
chat_session_id: str | None = Field(
    default=None,
    max_length=128,
    pattern=r"^[A-Za-z0-9._:-]+$",
)
```

Strategy path regex (`draft_generator.py:58`):

```python
_SAFE_PATH_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
```

The Pydantic pattern allows a leading `.`, `_`, or `-`; the strategy regex
requires the first character to be alnum.

**Failure scenario:** A client sends `chat_session_id=".foo"` or `"-foo"`. It
passes Pydantic validation, enters the dispatcher, then
`_safe_path_component(session_id, "session_id")` raises `invalid_session_id`
when the generation pipeline computes the draft dir → `400` draft failure for
an input the API accepted.

**Fix:** Tighten the Pydantic pattern to
`^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$` to match `_SAFE_PATH_COMPONENT` exactly.

---

### I6 — `/health` computes circuit-breaker status and discards it

**Location:** `aigateway-api/src/aigateway_api/routes.py:247`
**Severity:** P3 · **Confidence:** 9/10 · **Status:** verified

Lines 247-248:

```python
if litellm_bridge_for_cb is not None and hasattr(litellm_bridge_for_cb, "get_cooldown_status"):
    litellm_bridge_for_cb.get_cooldown_status()
```

The result is not assigned. The `dependencies` dict built below does not
include breaker state, and the response `data` never surfaces it. The comment
"构建熔断器状态" is misleading. This is residual dead code (the old code also
didn't include it, so there is no behavior change — just reader confusion).

**Fix:** Remove the call, or wire the result into the response `data` under a
`circuit_breaker` key.

---

### I7 — `DraftWorkflowConfig.draft_model` is dead config

**Location:** `aigateway-core/src/aigateway_core/pipelines/generation/_common/config.py:113`
**Severity:** P3 · **Confidence:** 9/10 · **Status:** verified

`DraftWorkflowConfig.draft_model` still defaults to `"agnes-image-2.1-flash"`
(`config.py:113`), but draft generation now uses
`ComfyUIConfig.checkpoint_name`:

- `draft_generator_plugin.py:168` reports `draft_model` as
  `f"comfyui:{self._strategy.checkpoint_name}"`.
- The strategy validates `ComfyUIConfig.checkpoint_name`, not
  `DraftWorkflowConfig.draft_model`.

Operators editing `draft_model` in `config.yaml` see no effect; the field is
dead config that still appears in the schema and config-ui, inviting
misconfiguration.

**Fix:** Remove `draft_model` from `DraftWorkflowConfig` and the schema
template, or repurpose it. Update any docs.

---

## Summary Table

| ID | Severity | Conf | File:Line | One-line |
|---|---|---|---|---|
| C1 | P1 | 9/10 | `draft_generator.py:1405` | Video confirm always fails (`video_enabled=False` default) while preview + button succeed |
| C2 | P1 | 9/10 | `draft_generator.py:2399` / `admin_routes.py:2779` | `delete_session` rejects ownerless drafts that `_assert_draft_owner` allows |
| I1 | P2 | 9/10 | `admin_routes.py:2986` | Dead `VideoSubmitResult` branch; video_id polling path orphaned |
| I2 | P2 | 8/10 | `draft_generator.py:821` | `os.walk` over models dir on every draft/confirm request |
| I3 | P2 | 8/10 | `draft_generator.py:1374` | Redis load+store held inside GPU semaphore |
| I4 | P3 | 8/10 | `draft_generator.py:2378` | One stray-named dir aborts session deletion with 500 |
| I5 | P3 | 9/10 | `openai_compat.py:71` | `chat_session_id` pattern looser than `_SAFE_PATH_COMPONENT` |
| I6 | P3 | 9/10 | `routes.py:247` | `/health` calls `get_cooldown_status()` and discards result |
| I7 | P3 | 9/10 | `_common/config.py:113` | `draft_model` config field unused; superseded by `checkpoint_name` |

---

## Recommendation

The mechanical type-annotation refactor is clean and tests pass. The real risk
is in the rewritten draft generator.

**Fix before merge:** C1 and C2 — both are user-facing regressions in the new
console-chat/draft flow (video confirm broken by default; legacy sessions
undeletable).

**Fix now (cheap, prevents future mis-wiring):** I1.

**Lower-stakes but cheap:** I2, I3 (perf), I4, I5 (validation/policy gaps).

**Cleanup:** I6, I7.

No fixes have been applied. Per workflow rule 0, all code changes remain in the
working tree until reviewed and explicitly approved.
