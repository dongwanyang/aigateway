# 聊天窗视频生成接通 Agnes /videos — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 聊天窗视频意图确认后走 Agnes `/videos` 真实视频生成,前端轮询到可播放 URL,渲染 `<video>` 播放器。

**Architecture:** `DraftGeneratorStrategy.confirm_draft` 按 `media_type` 分支:图片走原放大路径不变,视频调 `LiteLLMBridge._do_video_generation` 提交 Agnes `/videos` 任务,返回 `video_id`。`/admin/draft/{id}/confirm` 路由对视频返 `{video_id, status, media_type}`。前端 `confirmDraftMsg` 收到 video_id 后,把 draft 消息转成 video 消息(挂 `videoId`、清 `draft`),直接调 `pollVideoStatus` 轮询 `/v1/videos/{id}` 到 `videoUrl`,`MediaVideo` 渲染 `<video>`。

**Tech Stack:** Python 3.12 / FastAPI / dataclasses(后端);React + TypeScript(前端,Vite)。测试:pytest(后端单测)。

## Global Constraints

- 后端测试用 `python3 -m pytest`(无 `python` 别名)。
- 编辑安装:`aigateway-core/src`、`aigateway-api/src` 在 sys.path。
- 配置写必须原子(`_atomic_write_yaml`),本计划不改 config.yaml 结构。
- `DraftGeneratorStrategy` 的 bridge 已由 `main.py:566` 注入为 `self._litellm_bridge`,无需新增注入。
- 前端隐式 auth:`localStorage.aigateway_api_key`,API base `/aigateway`。
- 提交规范:每个任务一个 commit,消息 `fix(qa)/feat(qa): ...`,末尾加 `Co-Authored-By: AI 助手 Opus 4.8 (1M context) <noreply@AI 助手.com>`。
- **不自动提交。** 所有改动留在工作树,等用户审查后再提交(除非任务步骤明确写 commit)。

参考 spec:[docs/superpowers/specs/2026-07-24-chat-video-generation-agnes-design.md](../specs/2026-07-24-chat-video-generation-agnes-design.md)。

---

### Task 1: `DraftResult` 加 `video_id` 字段 + 序列化

**Files:**
- Modify: `aigateway-core/src/aigateway_core/pipelines/generation/_common/models.py:150-201`
- Modify: `aigateway-core/src/aigateway_core/pipelines/generation/draft/draft_generator.py:1704-1808`(`_store_draft` / `_load_draft`)
- Test: `tests/test_draft_generator_strategy.py`

**Interfaces:**
- Produces: `DraftResult.video_id: Optional[str] = None`;`_store_draft` 序列化 `video_id`,`_load_draft` 反序列化 `video_id`。

- [ ] **Step 1: 写失败测试**

在 `tests/test_draft_generator_strategy.py` 末尾加:

```python
@pytest.mark.asyncio
async def test_video_id_persists_through_store_load(strategy, video_request, default_config):
    """DraftResult.video_id 应能通过 _store_draft / _load_draft 往返。"""
    result = await strategy.generate_draft(video_request, default_config)
    draft = await _await_generating(strategy, result.draft_id)
    draft.video_id = "vid_abc123"
    await strategy._store_draft(draft, ttl_seconds=60)
    reloaded = await strategy._load_draft(draft.draft_id)
    assert reloaded is not None
    assert reloaded.video_id == "vid_abc123"
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python3 -m pytest tests/test_draft_generator_strategy.py::test_video_id_persists_through_store_load -v`
Expected: FAIL(`DraftResult` 无 `video_id` 字段,赋值报错,或反序列化后为 None)

- [ ] **Step 3: 给 `DraftResult` 加字段**

`aigateway-core/src/aigateway_core/pipelines/generation/_common/models.py` 的 `DraftResult` dataclass,在 `group_id` 之后加:

```python
    group_id: Optional[str] = None
    """视频任务提交后 Agnes 返回的 video_id,用于刷新后重新轮询 /v1/videos/{id}。仅 media_type=='video' 确认后有值。"""
    video_id: Optional[str] = None
```

- [ ] **Step 4: `_store_draft` 序列化 video_id**

`draft_generator.py` 的 `_store_draft` 方法,`serialized` 字典里(`"status": draft.status,` 那行之后)加:

```python
            "status": draft.status,
            "video_id": draft.video_id,
            "store_dir": draft_dir,  # 供 _load_draft 定位文件
```

- [ ] **Step 5: `_load_draft` 反序列化 video_id**

`_load_draft` 方法末尾 `return DraftResult(...)` 里(`group_id=data.get("group_id"),` 之后)加:

```python
            user_id=data.get("user_id"),
            group_id=data.get("group_id"),
            video_id=data.get("video_id"),
        )
```

- [ ] **Step 6: 跑测试验证通过**

Run: `python3 -m pytest tests/test_draft_generator_strategy.py::test_video_id_persists_through_store_load -v`
Expected: PASS

- [ ] **Step 7: 跑全部 draft 测试回归**

Run: `python3 -m pytest tests/test_draft_generator_strategy.py -q`
Expected: 全部 PASS(原有测试不受影响)

- [ ] **Step 8: 提交**

```bash
git add aigateway-core/src/aigateway_core/pipelines/generation/_common/models.py aigateway-core/src/aigateway_core/pipelines/generation/draft/draft_generator.py tests/test_draft_generator_strategy.py
git commit -m "feat(qa): DraftResult.video_id field + serialization

Co-Authored-By: AI 助手 Opus 4.8 (1M context) <noreply@AI 助手.com>"
```

---

### Task 2: 新增 `VideoSubmitResult` 数据类

**Files:**
- Modify: `aigateway-core/src/aigateway_core/pipelines/generation/_common/models.py`(在 `UpscaleResult` 之后)
- Test: `tests/test_draft_generator_strategy.py`

**Interfaces:**
- Produces: `VideoSubmitResult(draft_id: str, video_id: str, status: str = "generating")` — 与 `UpscaleResult` 平行,`confirm_draft` 视频分支返回它。

- [ ] **Step 1: 写失败测试**

在 `tests/test_draft_generator_strategy.py` 顶部 import 加 `VideoSubmitResult`:

```python
from aigateway_core.pipelines.generation._common.models import (
    DRAFT_STATUS_CONFIRMED,
    DRAFT_STATUS_GENERATING,
    DRAFT_STATUS_PENDING,
    DraftResult,
    GenerationRequest,
    UpscaleResult,
    VideoSubmitResult,
)
```

在文件末尾加:

```python
def test_video_submit_result_dataclass():
    """VideoSubmitResult 字段与默认值。"""
    r = VideoSubmitResult(draft_id="d1", video_id="vid_x")
    assert r.draft_id == "d1"
    assert r.video_id == "vid_x"
    assert r.status == "generating"
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python3 -m pytest tests/test_draft_generator_strategy.py::test_video_submit_result_dataclass -v`
Expected: FAIL(`ImportError: cannot import name 'VideoSubmitResult'`)

- [ ] **Step 3: 加 `VideoSubmitResult` 类**

`aigateway-core/src/aigateway_core/pipelines/generation/_common/models.py`,`UpscaleResult` 类定义之后(`PromptTemplate` 之前)加:

```python
@dataclass
class VideoSubmitResult:
    """视频任务提交结果.

    用户确认视频草稿后调 Agnes /videos 提交异步任务的输出。
    与 UpscaleResult 平行 —— confirm_draft 按 media_type 返回二者之一。

    Attributes:
        draft_id: 关联的草图标识
        video_id: Agnes /videos 返回的任务 id,前端据此轮询 GET /v1/videos/{id}
        status: 任务状态,提交成功后为 "generating"
    """

    draft_id: str
    video_id: str
    status: str = "generating"
```

- [ ] **Step 4: 跑测试验证通过**

Run: `python3 -m pytest tests/test_draft_generator_strategy.py::test_video_submit_result_dataclass -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add aigateway-core/src/aigateway_core/pipelines/generation/_common/models.py tests/test_draft_generator_strategy.py
git commit -m "feat(qa): VideoSubmitResult dataclass for video draft confirm

Co-Authored-By: AI 助手 Opus 4.8 (1M context) <noreply@AI 助手.com>"
```

---

### Task 3: `confirm_draft` 视频分支 + `_confirm_video_draft`

**Files:**
- Modify: `aigateway-core/src/aigateway_core/pipelines/generation/draft/draft_generator.py:395-470`(`confirm_draft`)
- Test: `tests/test_draft_generator_strategy.py`

**Interfaces:**
- Consumes: `DraftResult.video_id`(Task 1)、`VideoSubmitResult`(Task 2)、`self._litellm_bridge`(已注入)
- Produces: `confirm_draft(draft_id)` 返回 `UpscaleResult | VideoSubmitResult`;新增 `_confirm_video_draft(draft) -> VideoSubmitResult`

- [ ] **Step 1: 写失败测试**

在 `tests/test_draft_generator_strategy.py` 末尾加(用 `AsyncMock` mock bridge):

```python
@pytest.mark.asyncio
async def test_confirm_video_draft_calls_bridge_and_returns_video_id(strategy, video_request, default_config):
    """视频草稿确认后应调 bridge._do_video_generation,存 video_id,返回 VideoSubmitResult。"""
    result = await strategy.generate_draft(video_request, default_config)
    draft = await _await_generating(strategy, result.draft_id)
    assert draft.media_type == "video"

    # mock bridge: _do_video_generation 返回含 video_id 的结果
    from unittest.mock import AsyncMock
    strategy._litellm_bridge = AsyncMock()
    strategy._litellm_bridge._do_video_generation = AsyncMock(return_value={
        "_meta": {"video_id": "vid_test_123"},
        "usage": {},
    })

    out = await strategy.confirm_draft(draft.draft_id)
    assert isinstance(out, VideoSubmitResult)
    assert out.video_id == "vid_test_123"
    assert out.status == "generating"
    # bridge 被调用
    strategy._litellm_bridge._do_video_generation.assert_awaited_once()
    # video_id 持久化到 draft
    reloaded = await strategy._load_draft(draft.draft_id)
    assert reloaded is not None
    assert reloaded.video_id == "vid_test_123"
    assert reloaded.status == DRAFT_STATUS_CONFIRMED


@pytest.mark.asyncio
async def test_confirm_image_draft_still_returns_upscale_result(strategy, image_request, default_config):
    """图片草稿确认仍走放大路径,返回 UpscaleResult(回归保护)。"""
    result = await strategy.generate_draft(image_request, default_config)
    draft = await _await_generating(strategy, result.draft_id)
    assert draft.media_type == "image"

    from unittest.mock import AsyncMock
    strategy._litellm_bridge = AsyncMock()  # 图片路径不应调 _do_video_generation

    out = await strategy.confirm_draft(draft.draft_id)
    assert isinstance(out, UpscaleResult)
    assert not isinstance(out, VideoSubmitResult)
    strategy._litellm_bridge._do_video_generation.assert_not_called()
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python3 -m pytest tests/test_draft_generator_strategy.py::test_confirm_video_draft_calls_bridge_and_returns_video_id tests/test_draft_generator_strategy.py::test_confirm_image_draft_still_returns_upscale_result -v`
Expected: 第一个 FAIL(`confirm_draft` 返回 `UpscaleResult` 而非 `VideoSubmitResult`);第二个可能 PASS(回归)

- [ ] **Step 3: `confirm_draft` 加 media_type 分支**

`draft_generator.py` 的 `confirm_draft` 方法,在 `await self._store_draft(draft, ttl_remaining)` 之后、`# Upscale to target resolution` 注释之前插入分支:

```python
        await self._store_draft(draft, ttl_remaining)

        # 视频草稿:调 bridge 提交 Agnes /videos 异步任务,返回 video_id。
        # 不走图片放大路径。
        if draft.media_type == "video":
            return await self._confirm_video_draft(draft)

        # Upscale to target resolution via pixel-level super-resolution
        target_resolution = self._get_target_resolution(draft)
```

- [ ] **Step 4: 实现 `_confirm_video_draft`**

在 `confirm_draft` 方法之后(`reject_draft` 之前)加新方法:

```python
    async def _confirm_video_draft(self, draft: DraftResult) -> VideoSubmitResult:
        """视频草稿确认:调 bridge._do_video_generation 提交 Agnes /videos 任务。

        MVP 不传 input_reference(关键帧仅作预览确认,不强制作为视频首帧)。
        提交后把 video_id 存到 draft 上,供前端刷新后重新轮询。

        Args:
            draft: 已确认的视频草稿(media_type=='video')

        Returns:
            VideoSubmitResult 含 video_id

        Raises:
            DraftWorkflowError: bridge 未绑定或 Agnes /videos 调用失败
        """
        if self._litellm_bridge is None:
            raise DraftWorkflowError(
                "LiteLLM bridge not bound to DraftGeneratorStrategy; cannot submit video"
            )

        prompt = draft.generation_params.get("prompt", "")
        messages = [{"role": "user", "content": prompt}]

        # 模型解析:优先 generation_params 里的 model hint,否则按意图解析
        model = draft.generation_params.get("model")
        try:
            if not model:
                resolved = await self._litellm_bridge._resolve_by_intent(
                    intent="generation:video", model_hint=None
                )
                if "error" in resolved:
                    raise DraftWorkflowError(
                        f"video model resolution failed: {resolved['error']}"
                    )
                model = resolved["model"]

            vid_result = await self._litellm_bridge._do_video_generation(
                messages=messages, model=model
            )
        except DraftWorkflowError:
            raise
        except Exception as exc:
            raise DraftWorkflowError(f"Agnes /videos submission failed: {exc}") from exc

        video_id = (vid_result.get("_meta") or {}).get("video_id", "")
        if not video_id:
            raise DraftWorkflowError(
                "Agnes /videos returned no video_id"
            )

        # 持久化 video_id 到 draft,刷新后前端凭此重新轮询
        draft.video_id = video_id
        ttl_remaining = max(1, int(draft.expires_at - time.time()))
        await self._store_draft(draft, ttl_remaining)

        logger.info(
            "generation_optimization.draft_generator.video_submitted",
            extra={"draft_id": draft.draft_id, "video_id": video_id, "model": model},
        )

        return VideoSubmitResult(draft_id=draft.draft_id, video_id=video_id, status="generating")
```

确认文件顶部 import 有 `VideoSubmitResult`(Task 2 已加到 models,这里要在 `draft_generator.py` 的 import 补上)。找到 `draft_generator.py` 里 `from ..._common.models import` 那行,把 `VideoSubmitResult` 加进去:

```python
from aigateway_core.pipelines.generation._common.models import (
    ...,
    VideoSubmitResult,
)
```

- [ ] **Step 5: 跑两个新测试验证通过**

Run: `python3 -m pytest tests/test_draft_generator_strategy.py::test_confirm_video_draft_calls_bridge_and_returns_video_id tests/test_draft_generator_strategy.py::test_confirm_image_draft_still_returns_upscale_result -v`
Expected: 两个都 PASS

- [ ] **Step 6: 跑全部 draft 测试回归**

Run: `python3 -m pytest tests/test_draft_generator_strategy.py -q`
Expected: 全部 PASS

- [ ] **Step 7: 提交**

```bash
git add aigateway-core/src/aigateway_core/pipelines/generation/draft/draft_generator.py tests/test_draft_generator_strategy.py
git commit -m "feat(qa): confirm_draft video branch calls bridge _do_video_generation

Co-Authored-By: AI 助手 Opus 4.8 (1M context) <noreply@AI 助手.com>"
```

---

### Task 4: `/admin/draft/{id}/confirm` 路由按 media_type 分流返回

**Files:**
- Modify: `aigateway-api/src/aigateway_api/admin_routes.py:2783-2825`(`confirm_draft` 路由 handler)
- Test: `tests/test_draft_routes.py`

**Interfaces:**
- Consumes: `VideoSubmitResult`(Task 2)、`confirm_draft` 返回联合类型(Task 3)
- Produces: `POST /admin/draft/{id}/confirm` 对视频返 `{draft_id, video_id, status, media_type}`,对图片返原 `{draft_id, upscaled_url, target_resolution, algorithm, duration_ms}`

- [ ] **Step 1: 写失败测试**

先看 `tests/test_draft_routes.py` 现有 confirm 测试的 fixture/auth 模式,仿照写。在 `tests/test_draft_routes.py` 末尾加:

```python
@pytest.mark.asyncio
async def test_confirm_draft_video_returns_video_id(client, admin_headers, draft_strategy_with_video_draft):
    """视频草稿 confirm 应返回 {video_id, status, media_type},而非 upscaled_url。"""
    draft_id = draft_strategy_with_video_draft
    resp = await client.post(f"/admin/draft/{draft_id}/confirm", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["media_type"] == "video"
    assert body["video_id"] == "vid_test_123"
    assert body["status"] == "generating"
    assert "upscaled_url" not in body
```

fixture `draft_strategy_with_video_draft` 需要:mock `app.state.draft_generator_strategy` 返回一个 `VideoSubmitResult`。在 `tests/test_draft_routes.py` 里加 fixture(参考该文件已有的 draft fixture 写法;若没有现成 client+admin_headers fixture,参考 `tests/conftest.py` 或同文件其它测试的写法):

```python
@pytest.fixture
def draft_strategy_with_video_draft(monkeypatch):
    """注入一个 mock draft strategy,其 confirm_draft 对视频返 VideoSubmitResult。"""
    from aigateway_core.pipelines.generation._common.models import VideoSubmitResult
    from aigateway_api import app_state
    from unittest.mock import AsyncMock

    class _FakeDraft:
        user_id = None
        group_id = None

    strategy = AsyncMock()
    strategy.get_draft = AsyncMock(return_value=_FakeDraft())
    strategy.confirm_draft = AsyncMock(return_value=VideoSubmitResult(
        draft_id="d_video", video_id="vid_test_123", status="generating"
    ))
    monkeypatch.setattr(app_state, "_get_app_state", lambda app=None: type("S", (), {"draft_generator_strategy": strategy})())
    return "d_video"
```

> **注意:** 上述 fixture 是骨架。实现时先读 `tests/test_draft_routes.py` 已有的 client/admin_headers/draft fixture 写法,对齐它的 app 注入方式(`app_state._get_app_state` 或直接 `app.state`),再定稿。若该文件已有更简单的 mock 方式,优先用它的方式。

- [ ] **Step 2: 跑测试验证失败**

Run: `python3 -m pytest tests/test_draft_routes.py::test_confirm_draft_video_returns_video_id -v`
Expected: FAIL(路由仍返 `upscaled_url`,无 `media_type`/`video_id`)

- [ ] **Step 3: 修改 confirm 路由 handler**

`aigateway-api/src/aigateway_api/admin_routes.py` 的 `confirm_draft` 函数,把现有的:

```python
    try:
        upscale_result = await strategy.confirm_draft(draft_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail={"error": {"code": "draft_confirm_failed", "message": str(exc)}})

    output_data = upscale_result.output_data
    # 如果输出是 bytes，转为 base64 data URL
    if isinstance(output_data, bytes):
        b64 = base64.b64encode(output_data).decode("ascii")
        content_url = f"data:{_detect_image_mime(output_data)};base64,{b64}"
    else:
        content_url = str(output_data)[:500]
```

改为先判断是否视频:

```python
    try:
        result = await strategy.confirm_draft(draft_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail={"error": {"code": "draft_confirm_failed", "message": str(exc)}})

    # 视频草稿:返回 video_id,前端轮询 /v1/videos/{id}
    from aigateway_core.pipelines.generation._common.models import VideoSubmitResult
    if isinstance(result, VideoSubmitResult):
        try:
            from .openai_compat import _record_request_log
            await _record_request_log(
                request=request, method="POST", endpoint=f"/admin/draft/{draft_id}/confirm",
                status_code=200, duration_ms=0.0,
                model="agnes-video-v2.0", cache_hit=False, cache_tier=None,
            )
        except Exception as exc:
            logger.warning("视频草稿确认请求日志写入失败: %s", exc)
        return {
            "draft_id": draft_id,
            "video_id": result.video_id,
            "status": result.status,
            "media_type": "video",
        }

    # 图片草稿:放大结果转 base64 data URL(原逻辑)
    upscale_result = result
    output_data = upscale_result.output_data
    if isinstance(output_data, bytes):
        b64 = base64.b64encode(output_data).decode("ascii")
        content_url = f"data:{_detect_image_mime(output_data)};base64,{b64}"
    else:
        content_url = str(output_data)[:500]
```

(后面的图片返回逻辑 `return {"draft_id": ..., "upscaled_url": content_url, ...}` 保持不变)

- [ ] **Step 4: 跑新测试验证通过**

Run: `python3 -m pytest tests/test_draft_routes.py::test_confirm_draft_video_returns_video_id -v`
Expected: PASS

- [ ] **Step 5: 跑全部 draft routes 测试回归**

Run: `python3 -m pytest tests/test_draft_routes.py -q`
Expected: 全部 PASS(图片 confirm 测试仍过)

- [ ] **Step 6: 提交**

```bash
git add aigateway-api/src/aigateway_api/admin_routes.py tests/test_draft_routes.py
git commit -m "feat(qa): /admin/draft/{id}/confirm returns video_id for video drafts

Co-Authored-By: AI 助手 Opus 4.8 (1M context) <noreply@AI 助手.com>"
```

---

### Task 5: 前端 `confirmDraft` 返回联合类型

**Files:**
- Modify: `control-panel/src/api/client.ts:260-290`(`confirmDraft`)

**Interfaces:**
- Produces: `confirmDraft(draftId)` 返回 `{videoId, status, mediaType:'video'} | {upscaledUrl, targetResolution, algorithm, mediaType:'image'}`

- [ ] **Step 1: 修改 `confirmDraft` 返回类型与解析**

`control-panel/src/api/client.ts` 的 `confirmDraft` 函数,整体替换为:

```typescript
/** POST /admin/draft/{id}/confirm —— 确认草稿。
 * 图片:触发高清放大,返回 upscaled_url data URL。
 * 视频:提交 Agnes /videos 任务,返回 video_id(前端轮询 /v1/videos/{id})。 */
export type ConfirmDraftResult =
  | { videoId: string; status: string; mediaType: 'video' }
  | { upscaledUrl: string; targetResolution: [number, number]; algorithm: string; mediaType: 'image' }

export async function confirmDraft(draftId: string): Promise<ConfirmDraftResult> {
  const headers = await ensureAuthHeaders()
  const res = await fetch(`${API_BASE}/admin/draft/${encodeURIComponent(draftId)}/confirm`, {
    method: 'POST',
    headers,
  })
  if (!res.ok) {
    let code = `HTTP ${res.status}`
    try {
      const b = (await res.json()) as { error?: { code?: string; message?: string } }
      code = b.error?.code || b.error?.message || code
    } catch {
      // ignore
    }
    throw new Error(code)
  }
  const json = (await res.json()) as {
    media_type?: string
    video_id?: string
    status?: string
    upscaled_url?: string
    target_resolution?: [number, number]
    algorithm?: string
  }
  if (json.media_type === 'video' && json.video_id) {
    return {
      videoId: json.video_id,
      status: json.status ?? 'generating',
      mediaType: 'video',
    }
  }
  if (!json.upscaled_url) throw new Error('confirm 响应缺少 upscaled_url')
  return {
    upscaledUrl: json.upscaled_url,
    targetResolution: json.target_resolution ?? [0, 0],
    algorithm: json.algorithm ?? 'upscale',
    mediaType: 'image',
  }
}
```

- [ ] **Step 2: 类型检查**

Run: `cd control-panel && npx tsc --noEmit`
Expected: 无错误(`useChatSessions.ts` 里 `confirmDraftMsg` 解构 `{ upscaledUrl, targetResolution, algorithm }` 会在联合类型的 video 分支报错 —— 这是预期的,Task 6 修)

> **注意:** 这一步 `tsc` 会报 `useChatSessions.ts` 的解构错误,因为 `confirmDraft` 返回类型变了。这是正常的 —— Task 6 会修 `confirmDraftMsg`。**不要**为了过 tsc 而回退本任务的改动。若 tsc 报的是 `client.ts` 自身错误,才需修。

- [ ] **Step 3: 提交(与 Task 6 一起提交,或单独提交 client.ts)**

```bash
git add control-panel/src/api/client.ts
git commit -m "feat(qa): confirmDraft returns union type for video/image

Co-Authored-By: AI 助手 Opus 4.8 (1M context) <noreply@AI 助手.com>"
```

---

### Task 6: 前端 `confirmDraftMsg` 视频分流 + 触发轮询

**Files:**
- Modify: `control-panel/src/hooks/useChatSessions.ts:774-799`(`confirmDraftMsg`)

**Interfaces:**
- Consumes: `confirmDraft` 联合返回类型(Task 5)、`pollVideoStatus`(已存在,line 821)

- [ ] **Step 1: 修改 `confirmDraftMsg` 按返回类型分流**

`control-panel/src/hooks/useChatSessions.ts` 的 `confirmDraftMsg` 函数,整体替换为:

```typescript
  const confirmDraftMsg = useCallback(async (msgId: string) => {
    const s = sessions.find(x => x.id === activeId)
    const msg = s?.messages.find(m => m.id === msgId)
    if (!msg?.draft) return
    // 防连点:status 已是 confirming/rejecting 时直接返回(按钮 disable 依赖 re-render,有窗口期)。
    if (msg.draft.status === 'confirming' || msg.draft.status === 'rejecting') return
    patchMessage(msgId, m => m.draft ? { ...m, draft: { ...m.draft, status: 'confirming', errorMessage: undefined } } : m)
    try {
      const result = await confirmDraft(msg.draft.draftId)
      if (result.mediaType === 'video') {
        // 视频草稿确认:提交 Agnes /videos 任务拿到 video_id。
        // 把 draft 消息转成 video 消息(清 draft,挂 videoId),直接启动轮询。
        // 不依赖 resumePollingKey effect(那个只在 session 切换/加载时触发)。
        patchMessage(msgId, m => ({
          ...m,
          draft: undefined,
          videoId: result.videoId,
          intent: 'generation:video',
          model: 'video',
        }))
        void pollVideoStatus(result.videoId, msgId)
      } else {
        // 图片草稿确认:高清放大结果挂到 draft.resultDataUrl。
        patchMessage(msgId, m => m.draft
          ? { ...m, draft: { ...m.draft, status: 'confirmed', resultDataUrl: result.upscaledUrl, errorMessage: undefined } }
          : m)
      }
    } catch (e) {
      const code = e instanceof Error ? e.message : '确认失败'
      const expired = code.includes('expired') || code.includes('not_found')
      patchMessage(msgId, m => m.draft
        ? { ...m, draft: { ...m.draft, status: expired ? 'expired' : 'error', errorMessage: code } }
        : m)
    }
  }, [sessions, activeId, patchMessage, pollVideoStatus])
```

- [ ] **Step 2: 类型检查**

Run: `cd control-panel && npx tsc --noEmit`
Expected: 无错误

- [ ] **Step 3: 提交**

```bash
git add control-panel/src/hooks/useChatSessions.ts
git commit -m "feat(qa): confirmDraftMsg video branch sets videoId + polls

Co-Authored-By: AI 助手 Opus 4.8 (1M context) <noreply@AI 助手.com>"
```

---

### Task 7: `DraftCard` 未确认视频预览改用图片显示

**Files:**
- Modify: `control-panel/src/components/chat/DraftCard.tsx:50-75`

- [ ] **Step 1: 修改未确认视频预览渲染**

`control-panel/src/components/chat/DraftCard.tsx` 第 50-75 行附近,把未确认视频 draft 的 `<video>` 分支改成用 `ImageLightbox` 显示关键帧(和图片一致)。定位到:

```tsx
      {draft.status === 'confirmed' && draft.resultDataUrl ? (
        <ImageLightbox src={draft.resultDataUrl} alt="高清结果" thumbAlt="高清结果(点击放大)" />
      ) : draft.previewDataUrl ? (
        draft.mediaType === 'video' ? (
          <video
            src={draft.previewDataUrl}
            controls
            style={{ maxWidth: '100%', borderRadius: 6, border: '1px solid var(--color-border)' }}
          />
        ) : (
          <ImageLightbox src={draft.previewDataUrl} alt="草稿预览" thumbAlt="草稿预览(点击放大)" />
        )
      ) : !terminal ? (
```

改为(视频未确认时也用 ImageLightbox 显示首帧,标签区分):

```tsx
      {draft.status === 'confirmed' && draft.resultDataUrl ? (
        <ImageLightbox src={draft.resultDataUrl} alt="高清结果" thumbAlt="高清结果(点击放大)" />
      ) : draft.previewDataUrl ? (
        <ImageLightbox
          src={draft.previewDataUrl}
          alt={draft.mediaType === 'video' ? '视频首帧预览' : '草稿预览'}
          thumbAlt={draft.mediaType === 'video' ? '视频首帧预览(点击放大)' : '草稿预览(点击放大)'}
        />
      ) : !terminal ? (
```

> 视频确认后不再走 DraftCard(confirmDraftMsg 已把 draft 清空,转成 video 消息走 MediaVideo),所以 `draft.status === 'confirmed'` 分支对视频不会命中,但保留无害。

- [ ] **Step 2: 类型检查**

Run: `cd control-panel && npx tsc --noEmit`
Expected: 无错误

- [ ] **Step 3: 提交**

```bash
git add control-panel/src/components/chat/DraftCard.tsx
git commit -m "fix(qa): DraftCard video preview shows keyframe as image, not broken <video>

Co-Authored-By: AI 助手 Opus 4.8 (1M context) <noreply@AI 助手.com>"
```

---

### Task 8: 端到端浏览器验证 + 重建镜像

**Files:** 无代码改动,验证 only。

- [ ] **Step 1: 重建 gateway 镜像(后端 Python 改动)**

Run:
```bash
sudo DOCKER_BUILDKIT=1 docker compose up -d --build gateway
sleep 8
curl -sf localhost:8000/health | python3 -c "import sys,json; print('gateway:', json.load(sys.stdin)['data']['status'])"
```
Expected: `gateway: healthy`

- [ ] **Step 2: 重建 control-panel 镜像(前端改动)**

Run:
```bash
sudo DOCKER_BUILDKIT=1 docker compose up -d --build control-panel
sleep 5
curl -sf -o /dev/null -w "panel HTTP %{http_code}\n" http://localhost:3000/
```
Expected: `panel HTTP 200`

- [ ] **Step 3: 检查 gateway 启动日志无错误**

Run:
```bash
sudo docker compose logs --tail=50 gateway 2>&1 | grep -iE "error|traceback|exception" | head -10
```
Expected: 无输出(或仅有非相关 warning)

- [ ] **Step 4: 浏览器实测视频生成**

用 browse 工具(见下方 QA 验证脚本),在 http://localhost:3000/chat:
1. 确保 `localStorage.aigateway_api_key` 已设(admin key)
2. 发送 `生成一段日落海面的视频`
3. 等草稿预览出现 → 点"确认放大"
4. 预期:不再显示高清图片,而是显示视频生成中状态 → 最终渲染 `<video controls>` 播放器

验证命令(用 browse CLI):
```bash
B="$HOME/.claude/skills/gstack/browse/dist/browse"
$B goto http://localhost:3000/chat
$B js "localStorage.setItem('aigateway_api_key', '<admin key>'); location.href='/chat'"
# 填 prompt → 发送 → 等草稿 → 点确认 → 检查 DOM 是否出现 <video>
$B snapshot -C
$B js "!!document.querySelector('video')"
```
Expected: 最后一步返回 `true`(页面有 `<video>` 元素)

- [ ] **Step 5: 若验证失败,记录现象**

若 `<video>` 未出现,记录:gateway 日志(`docker compose logs gateway`)、浏览器 console(`$B console --errors`)、实际渲染的 DOM(`$B snapshot -C`)。回报告,不擅自改代码(交用户决策)。

- [ ] **Step 6: 跑全部后端单测回归**

Run: `python3 -m pytest tests/ -q`
Expected: 全部 PASS(e2e/ui 自动跳过)

- [ ] **Step 7: 更新 CLAUDE.md(若架构有变)**

检查 CLAUDE.md 的 "Known States & Gotchas" / 架构图是否需要补一句"视频意图确认后走 Agnes /videos"。若超 300 行先 prune。本次改动小,大概率只需在 "Draft-to-HiRes" gotcha 条目补一句。

```bash
wc -l CLAUDE.md
```
按需 Edit。

- [ ] **Step 8: 报告**

向用户报告:验证结果(成功/失败 + 证据截图路径)、健康分变化、提交列表。

---

## Self-Review

**1. Spec 覆盖:**
- 后端 `confirm_draft` 分支 + `_confirm_video_draft` → Task 3 ✓
- `VideoSubmitResult` → Task 2 ✓
- `DraftResult.video_id` + 序列化 → Task 1 ✓
- `/admin/draft/{id}/confirm` 路由分流 → Task 4 ✓
- 前端 `confirmDraft` 联合类型 → Task 5 ✓
- 前端 `confirmDraftMsg` 分流 + 触发轮询 → Task 6 ✓
- `DraftCard` 视频预览修正 → Task 7 ✓
- `MessageBubble`/`MediaVideo` 无需改(spec 已确认)→ 计划未改,正确 ✓
- 错误处理(`DraftWorkflowError` → 400 → 前端 error)→ Task 3 `_confirm_video_draft` 抛错 + Task 4 路由 catch + Task 6 catch 复用现有 ✓
- 刷新恢复(`video_id` 持久化 + 前端 resume)→ Task 1 持久化 + 前端已有 `pollVideoStatus` resume effect(line 868-888)✓
- 浏览器实测 → Task 8 ✓

**2. 占位符扫描:** Task 4 的 fixture 有"参考已有写法"的说明 —— 这是因 `test_draft_routes.py` 的 app 注入方式需实现时核对,已明确标注核对对象,非空泛占位。其余步骤均有完整代码。

**3. 类型一致性:**
- `VideoSubmitResult(draft_id, video_id, status)` — Task 2 定义,Task 3 返回,Task 4 isinstance 判断,一致 ✓
- `DraftResult.video_id` — Task 1 定义,Task 3 读写,一致 ✓
- `confirmDraft` 联合类型 `mediaType:'video'|'image'` — Task 5 定义,Task 6 消费,一致 ✓
- `pollVideoStatus(videoId, msgId)` — 已存在(line 821),Task 6 直接调,签名一致 ✓

无问题。
