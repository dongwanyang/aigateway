# 草稿会话级文件存储设计

日期: 2026-07-21
状态: Draft(待 review)

## 背景

当前草稿(Draft)存储全部内嵌在 `DraftGeneratorStrategy` 的私有方法里,用 Redis key
`aigateway:draft:{draft_id}`,TTL 24h。问题:

1. **TTL 到期即丢**:草稿数据(预览图 bytes、状态)放 Redis,24h 后被清。刷新页面后,
   `getDraftPreview` 拿不到数据 → 前端显示"草稿已过期"。用户期望"会话持续期间一直保留"。
2. **高清图(result)根本不持久化**:`confirm_draft` 的 `UpscaleResult.output_data` 只在
   HTTP 响应里返回一次,刷新后无法重取 → 前端只能 `resultLost` 降级显示"刷新后仅预览"。
3. **草稿与会话无关联**:`DraftResult` 没有 session_id 字段,无法按会话清理。
4. **ownership 校验 fail-closed**:`admin_routes.py` confirm/reject 读 `draft_data.metadata`,
   但 `DraftResult` 根本没有 `metadata` 字段 → 永远 403。当前前端 confirm 会失败。

## 目标

- 草稿(预览图 + 高清图 + 状态)存**文件系统**,按 `chat_session_id` 分区。
- 会话持续期间一直保留;会话关闭时清理。
- 高清图持久化,刷新后可重取。
- 修复 ownership 校验(fail-closed → 正确放行)。
- 后端定时清理 + 长 TTL 兜底(防前端不调用清理)。

## 非目标

- 不做多设备/多标签实时同步(草稿仍按设备本地会话,文件存储天然跨标签共享同设备)。
- 不改 Redis 中其他缓存(L1/L2/L3 命中缓存与草稿无关)。
- 不改视频轮询逻辑(视频任务仍走 `GET /v1/videos/{id}`)。

## 决策(已与用户确认)

| 决策点 | 选择 |
|---|---|
| 会话身份 | **前端 session id**(`sess-xxx`,随草稿请求带给后端) |
| 高清图持久化 | **是**,存文件,新增 `GET /admin/draft/{id}/result` 重取 |
| 清理触发 | **后端定时清理 + 长 TTL 兜底**(不依赖前端主动调 DELETE) |

## 架构

### 存储布局

```
/data/drafts/{chat_session_id}/
  ├── {draft_id}.meta.json      # 草稿元数据(状态、params、expires_at、user_id、created_at)
  ├── {draft_id}.preview.png    # 预览图原始 bytes(图片单张;视频首帧)
  └── {draft_id}.result.png     # 高清放大结果(confirm 后写入;未确认时不存在)
```

- `chat_session_id` = 前端 `useChatSessions` 生成的 `sess-{timestamp}-{rand}`。
- 目录按 session 分区 → 清理 = 删目录。
- meta.json 不含图片 bytes(避免大 JSON),图片单独存文件。
- 兜底 TTL:`expires_at` 写入 meta;定时任务扫描,过期删整个 session 目录。

### 数据流

```
前端 send()
  │ body 加 chat_session_id(非标准字段,OpenAI 忽略,后端 dispatcher 读取)
  ▼
openai_compat._handle_chat_completion
  │ 从 body 取 chat_session_id → 挂到 PipelineContext.extra["chat_session_id"]
  ▼
dispatcher → PipelineEngine[generation] → DraftGeneratorPlugin.execute(ctx)
  │ ctx.extra["chat_session_id"] 透传给 strategy.generate_draft(session_id=...)
  ▼
DraftGeneratorStrategy.generate_draft(session_id, ...)
  │ 生成预览 → 写 /data/drafts/{session_id}/{draft_id}.meta.json + .preview.png
  │ 返回 DraftResult(draft_id, preview_url="/admin/draft/{id}/preview")
  ▼
openai_compat 把 draft_id + preview_url 包成 application/json 返回前端
  │
  ├─ 前端 getDraftPreview(draftId) → GET /admin/draft/{id}/preview
  │    后端读 .preview.png → base64 data URL 返回(不触达 LLM)
  │
  ├─ 前端 confirmDraft(draftId) → POST /admin/draft/{id}/confirm
  │    后端 ownership 校验(meta.user_id vs auth.user_id)→ 放大 → 写 .result.png
  │    → 返回 upscaled_url(data URL)+ 高清图落盘
  │
  └─ 前端刷新 → resume effect → getDraftPreview + 新增 getDraftResult(若有)
       重取预览/高清图,不重发 LLM 请求
```

### 新增/改动端点

| 端点 | 改动 |
|---|---|
| `POST /v1/chat/completions` | body 透传 `chat_session_id` 到 PipelineContext.extra |
| `GET /admin/draft/{id}/preview` | 改为读文件 `.preview.png`(不再读 Redis) |
| `GET /admin/draft/{id}` (status) | 改为读 meta.json |
| `POST /admin/draft/{id}/confirm` | 改为读 meta.json + ownership;放大后写 `.result.png`;返回 upscaled_url |
| `POST /admin/draft/{id}/reject` | 改为读 meta.json + ownership;删旧 draft 文件;生成新 draft |
| **`GET /admin/draft/{id}/result`** | **新增**:读 `.result.png` → base64 data URL。已确认但前端刷新后丢失高清图时重取 |
| **`DELETE /admin/drafts/session/{session_id}`** | **新增**:删整个 session 目录(前端关闭会话时主动调;兜底由定时任务覆盖) |

### Ownership 校验修复

`DraftResult` / meta.json 新增 `user_id` + `group_id` 字段(从 PipelineContext.user_id 写入)。
confirm/reject 改为读 meta 的 user_id,与 `authenticate_admin` 返回的 user_id 比对。
有 user_id 且不匹配 → 403;user_id 匹配或均为 admin → 放行。**移除"无 owner metadata 即 403"
的 fail-closed**,改为"有 owner 就校验,无 owner(admin 创建)放行"。

### 定时清理

- 后台 asyncio task,每 1 小时扫描 `/data/drafts/*/`,读各 session 目录下 meta.json 的
  `expires_at`,过期则删整个 session 目录。
- 兜底 TTL:默认 7 天(配置项 `draft_session_ttl_hours`,范围 1-720)。比单草稿 TTL 长,
  覆盖"前端关闭浏览器没调 DELETE"的场景。
- 前端关闭会话时主动调 `DELETE /admin/drafts/session/{session_id}` 即时清理。

## 改动清单

### 后端(aigateway-core)

1. **`_common/models.py`**:`DraftResult` 加 `session_id: str`、`user_id: Optional[str]`、
   `group_id: Optional[str]` 字段。
2. **`_common/config.py`**:加 `draft_session_ttl_hours: int = 168`(7 天)。
3. **`draft/draft_generator.py`**:
   - `generate_draft` 加 `session_id`、`user_id`、`group_id` 参数,写入 DraftResult。
   - 新增 `DraftFileStore`(或内嵌方法):`_store_draft` / `_load_draft` / `_delete_draft`
     改为文件读写(meta.json + .preview.png + .result.png),不再用 Redis。
   - `confirm_draft`:放大后写 `.result.png`。
   - `reject_draft`:删旧 draft 文件,新 draft 写入同 session 目录。
   - 保留 `redis_client` 注入用于其他用途(不删),但草稿存储不再用它。
4. **`draft/draft_generator_plugin.py`**:`execute` 从 `ctx.extra["chat_session_id"]`、
   `ctx.user_id`、`ctx.group_id` 取值传给 `generate_draft`。
5. **新增 `draft/draft_cleaner.py`**:后台定时扫描清理任务,在 main.py lifespan 启动。

### 后端(aigateway-api)

6. **`openai_compat.py`**:`_handle_chat_completion` 从 request body 取 `chat_session_id`,
   挂到 PipelineContext.extra。
7. **`admin_routes.py`**:
   - `get_draft_status` / `get_draft_preview` / `confirm_draft` / `reject_draft`:改读文件。
   - ownership 校验改用 meta.user_id(修复 fail-closed)。
   - 新增 `GET /admin/draft/{id}/result`。
   - 新增 `DELETE /admin/drafts/session/{session_id}`。

### 前端(control-panel)

8. **`api/client.ts`**:
   - `requestChatCompletion` body 加 `chat_session_id`。
   - 新增 `getDraftResult(draftId)` → `GET /admin/draft/{id}/result`。
   - 新增 `deleteSessionDrafts(sessionId)` → `DELETE /admin/drafts/session/{id}`。
9. **`hooks/useChatSessions.ts`**:
   - `send` 传 `chat_session_id: activeId`。
   - resume effect:confirmed 草稿调 `getDraftResult` 重取高清图(替代 `resultLost` 降级)。
   - `deleteSession` / `clearActive`:调 `deleteSessionDrafts(sessionId)` 清理后端文件。
10. **`types.ts`**:`ChatDraftState` 去掉 `resultLost`(或保留为兼容,但 resume 时优先重取)。

## 测试计划

- 单测:`DraftFileStore` 写/读/删;meta.json 序列化;过期清理。
- 集成:发图片请求 → 草稿落盘 → 刷新 → preview/result 重取不重发 LLM → confirm → 高清图落盘 →
  刷新 → 高清图重取 → 关闭会话 → 目录删除。
- /qa:浏览器验证刷新/切会话/关闭会话三场景。

## 风险

- **session id 全局唯一性**:前端 `sess-{ts}-{rand}` 理论上可能撞,但概率极低;且按 user_id
  分区(`/data/drafts/{user_id}/{session_id}/`)可进一步隔离。**待定:是否加 user_id 前缀。**
- **磁盘占用**:高清图 ~数 MB/张,7 天 TTL 下可能累积。定时清理 + 前端主动删可控。
- **并发**:同 session 多 draft 并发写不同 draft_id 文件,无冲突;同 draft_id 并发 confirm
  需文件锁(或状态机校验,现有 `status` 已防重)。
