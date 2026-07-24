# 聊天窗视频生成接通 Agnes /videos — 设计

- **日期:** 2026-07-24
- **分支:** feature/intent-driven-routing
- **状态:** 待评审

## 背景

QA(2026-07-24)发现:聊天窗发"生成视频",意图分类正确(`generation:video`),但最终产出的是一张高清图片,不是可播放的视频。

根因:chat 流对 `generation:video` 走 draft 生成器 → 用户确认 → 图片放大(`confirm_draft` → `UpscaleResult`)这条路径,**从未调用** bridge 的真实视频生成(`_do_video_generation` / `POST /videos` / `video_id` 轮询)。本次提交(commit f00f46d)给 `MediaVideo` 加的 `videoId`/`videoUrl` props 和 `getVideoStatus` 轮询因此处于休眠状态。spec(`2026-07-19-control-panel-chat-window-v2-design.md` 第 63 行)把"视频草稿确认后的播放器增强"列为未来工作。

原规划是 ComfyUI 关键帧渐进合成(`preview_keyframe_interval_seconds=5`、AnimateDiff/LTX-Video),但本环境未部署 ComfyUI,关键帧生成走占位降级。同时 bridge 里已有现成可用的 Agnes `/videos` 视频路径(`_do_video_generation` → `POST /videos` → `video_id` → `GET /v1/videos/{id}` 轮询),只是聊天流没接。

## 目标

聊天窗视频意图确认后,走 Agnes `/videos` 真实视频生成,前端轮询到可播放 URL,渲染 `<video>` 播放器。ComfyUI 关键帧渐进合成留后续。

## 决策

1. **引擎:** Agnes `/videos` API(bridge 里已写好,现成可用)。ComfyUI 关键帧合成留后续。
2. **预览确认门:** 保留。用户发"生成视频"→ 草稿阶段用 Agnes Images 生成关键帧预览 → 用户确认 → 提交 Agnes `/videos` 任务 → 轮询到可播放 URL。
3. **confirm 接口形态:** 视频确认立即返 `video_id`(Agnes `/videos` 异步,生成需几十秒),前端轮询。图片确认仍同步返 `upscaled_url`。
4. **关键帧 input_reference:** MVP 不传 `input_reference`,让 Agnes 仅凭 prompt 生成视频。关键帧只作预览确认用,不强制作为视频首帧。避开 data URL 兼容性风险。

## 流程

```
用户: "生成一段日落海面的视频"
  → classify_request → generation:video                  (不变)
  → generation engine: draft_generator 提交草稿
     → preview = Agnes Images API (agnes-image-2.1-flash), 1 关键帧
     → 返回 draft_pending_confirmation                    (不变)
  → DraftCard 渲染关键帧预览 + "确认放大"
  → 用户点确认
  → POST /admin/draft/{id}/confirm
     → confirm_draft 见 media_type=='video'
        → LiteLLMBridge._do_video_generation(
            model=agnes-video-v2.0, messages=[{prompt}], 无 input_reference)
        → Agnes 返回 {id, status:'queued'}
        → video_id 存到 draft, draft.status='confirmed'
     → 响应: {video_id, status:'generating', media_type:'video'}  (非 upscaled_url)
  → 前端: draft 消息 → video 消息 (videoId 挂上, draft 清空, intent='generation:video')
  → MediaVideo 轮询 GET /v1/videos/{id} (3s 间隔, 5 分钟超时)
  → succeeded/completed → set videoUrl → 渲染 <video controls>
```

图片流程不变:`confirm_draft` media_type=='image' → 放大 → `{upscaled_url}`。

## 后端改动

### 1. `DraftGeneratorStrategy.confirm_draft` 分支 ([draft_generator.py:395](../../../../aigateway-core/src/aigateway_core/pipelines/generation/draft/draft_generator.py))

现有 status/过期校验不变。状态置 confirmed 后,按 media_type 分流:

```python
if draft.media_type == "video":
    return await self._confirm_video_draft(draft)   # 新增
# 图片放大路径完全不变
```

新增 `_confirm_video_draft(draft)`:
- 用 `self._litellm_bridge`（已由 main.py:566 注入）调 `_do_video_generation`
- 模型解析:优先 `draft.generation_params` 里存的 model hint,否则 `self._litellm_bridge._resolve_by_intent(intent="generation:video")` 解析 `agnes-video-v2.0`
- `messages=[{"role":"user","content": draft.generation_params["prompt"]}]`
- 不传 `input_reference`（MVP）
- 调 `self._litellm_bridge._do_video_generation(messages=..., model=...)`
- `video_id = result["_meta"]["video_id"]`
- 存到 `draft.video_id`（新字段）,持久化 meta/Redis
- 返回 `VideoSubmitResult(draft_id, video_id, status="generating")`

### 2. 新增 `VideoSubmitResult` 数据类

与 `UpscaleResult` 平行。字段:`draft_id: str`、`video_id: str`、`status: str = "generating"`。放在 `_common/models.py`。

### 3. `DraftResult` 加字段 ([_common/models.py:157](../../../../aigateway-core/src/aigateway_core/pipelines/generation/_common/models.py))

`video_id: Optional[str] = None`。让提交后的任务在刷新/重载后仍能恢复。`_store_draft` / `_load_draft` 序列化时带上此字段。

### 4. `/admin/draft/{id}/confirm` 路由 ([admin_routes.py:2739](../../../../aigateway-api/src/aigateway_api/admin_routes.py))

返回按 media_type 分流:

```python
result = await strategy.confirm_draft(draft_id)
if hasattr(result, 'video_id'):   # VideoSubmitResult
    return {"draft_id": draft_id, "video_id": result.video_id,
            "status": "generating", "media_type": "video"}
# 图片路径不变: {upscaled_url, target_resolution, algorithm, duration_ms}
```

请求日志仍记（confirm 路由原本就记），model 记 `agnes-video-v2.0`。

## 前端改动

### 1. `confirmDraft` 返回联合类型 ([client.ts:261](../../../../control-panel/src/api/client.ts))

```ts
const json = await res.json()
if (json.media_type === 'video' && json.video_id) {
  return { videoId: json.video_id, status: 'generating', mediaType: 'video' as const }
}
if (!json.upscaled_url) throw new Error('confirm 响应缺少 upscaled_url')
return { upscaledUrl: json.upscaled_url, mediaType: 'image' as const }
```

返回类型:`{videoId, status, mediaType:'video'} | {upscaledUrl, mediaType:'image'}`。

### 2. `useChatSessions` confirm 成功后分流 ([useChatSessions.ts:833 附近](../../../../control-panel/src/hooks/useChatSessions.ts))

- 图片:`patchMessage` 设 `draft.resultDataUrl = upscaledUrl`,draft.status='confirmed'（现状不变）
- 视频:`patchMessage` 把消息从 draft 转成 video:
  ```ts
  patchMessage(msgId, m => ({
    ...m,
    videoId: result.videoId,
    draft: undefined,               // 清 draft,不再走 DraftCard
    intent: 'generation:video',
    model: 'video',
  }))
  ```
  触发 `MediaVideo` 轮询（复用本次提交的 `videoId` 轮询逻辑 + `getVideoStatus`,3s 间隔,5 分钟超时）。

### 3. `DraftCard` 视频分支修正 ([DraftCard.tsx:57](../../../../control-panel/src/components/chat/DraftCard.tsx))

现状:未确认的视频 draft 把 PNG 关键帧 bytes 塞进 `<video src>`——播不了。改为:未确认视频 draft 用 `<ImageLightbox>` 显示关键帧图片（和图片 draft 一致）,标签标"视频首帧预览"。`<video>` 只在确认后、`videoUrl` 就绪时由 `MediaVideo` 渲染。

### 4. `MessageBubble` / `MediaVideo` — 无需改

- [MessageBubble.tsx:16](../../../../control-panel/src/components/chat/MessageBubble.tsx) `intent==='generation:video'` → `kind='video'` → 走 `MediaVideo` 分支,已传 `videoId`/`videoUrl`。confirm 后把 `intent` 设为 `generation:video` 即可。
- `MediaVideo` 本次提交已写好 `videoId`/`videoUrl` props + `getVideoStatus` 轮询 + 接受 `succeeded`/`completed` 终态。无需改。

## 错误处理

- `_do_video_generation` 调用失败（Agnes 5xx/超时）→ `confirm_draft` 抛 `DraftWorkflowError` → 路由返 400 `draft_confirm_failed` → 前端 `draft.status='error'` + "重新生成"按钮（现状已有）
- 轮询 `failed`/`error`/`expired` → `MediaVideo` phase='failed'（本次提交已加）
- 轮询超时（5 分钟）→ phase='timeout'
- 刷新恢复:`DraftResult.video_id` 持久化,前端加载时若消息有 `videoId` 且无 `videoUrl` → 重新启动轮询（复用 `useChatSessions` 现有 `hasActiveAsyncTask` 逻辑）

## 测试

- 单测:`_confirm_video_draft` mock bridge,断言调了 `_do_video_generation`、存了 `video_id`、返回 `VideoSubmitResult`
- 单测:`confirm_draft` media_type='image' 仍走 upscale（回归保护）
- 单测:`/admin/draft/{id}/confirm` 路由对 video 返 `{video_id, status, media_type}`
- 浏览器实测:发"生成视频"→确认→看到 `<video>` 播放器（不再是 `<img>`）

## 范围边界

**本次做:** 聊天窗视频意图确认后走 Agnes `/videos` 真实视频生成 + 前端轮询播放。
**本次不做（留后续）:** ComfyUI 关键帧渐进合成;视频首帧强制对齐（input_reference）;视频任务后端进度推送（SSE）。
