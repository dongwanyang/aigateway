# 控制台聊天窗 v2:布局 / 多会话 / 刷新续传 / 草稿渲染 设计

**Status:** Draft
**Date:** 2026-07-19
**Author:** Gateway2 team via brainstorming skill
**Related:** `docs/superpowers/specs/2026-07-18-control-panel-chat-window-mvp-design.md`(v1 MVP,本 spec 修正其与后端的契约脱节并扩展功能)

---

## 1. 背景与问题

v1 聊天窗 MVP(`docs/superpowers/specs/2026-07-18-control-panel-chat-window-mvp-design.md`)已落地(commit `951f1d4`),但实测存在两类问题:

### 问题 A(主因):图片/视频生成请求永远没有输出

v1 spec 第 3 节假设:生成意图(`generation:image/video`)时,后端 `/v1/chat/completions`(`stream:true`)会返回**单个 SSE chunk**,内含裸图片 URL 或 job id。**该假设与后端实现脱节。**

实际后端(`aigateway-core/src/aigateway_core/dispatch/dispatcher.py:668-716`)对生成意图走的是 **Draft-to-HiRes 草稿确认工作流**:当 `draft_generator` 插件判定适用时,后端**不走流式出口**,而是直接返回一个**完整 JSON 响应**:

```json
{
  "data": {
    "draft_id": "94e8df53...",
    "preview_url": "/admin/draft/{id}/preview",
    "generation_params": {"prompt":"...","target_resolution":[1920,1080],"media_type":"image",...}
  },
  "_meta": {"draft_pending_confirmation": true}
}
```

- `content-type: application/json`(非 `text/event-stream`),`content-length` 固定且小。
- 后端草稿预览实际已生成成功(`GET /admin/draft/{id}/preview` 返回 base64 PNG data URL,实测可用)。

前端 `useChat.ts` 无条件按 SSE 流处理(`reader.read()` + 按 `\n\n` 切帧 + 找 `data:` 前缀)。该 JSON body 内无 `data:` 帧,于是:
- 解析循环一直匹配不到有效帧 → 助手消息内容始终为空 → UI 永远显示流式光标 ▌ → 用户看到"很久没有输出"。
- fetch body 读完 → `reader.read()` 返回 `done:true` → 循环静默退出(`streaming=false`)→ 留下一条**空助手消息**。无报错,难察觉。

### 问题 B:刷新后会话"断了"

v1 spec 非目标里明确"断连即取消"。刷新会中断进行中的 SSE fetch。叠加问题 A,用户刷新前根本没收到任何内容,空助手消息还残留在 localStorage(`aigateway:chat:messages`),观感即"会话断了"。

### 问题 C(用户提出的新需求)

1. 聊天页应**保留侧栏/顶栏**(当前 v1 是全屏,隐藏导航)。
2. 支持**创建新聊天窗口**(多会话)。
3. 刷新后**会话不断**。

## 2. 目标与非目标

### 目标

1. **布局**:聊天页保留 Layout 顶栏 + 侧栏,不再全屏;聊天区在主内容区内自适应高度。
2. **多会话**:会话列表 + 新建;聊天页内常驻二级会话列;支持切换/删除;localStorage 持久化所有会话。
3. **刷新续传**:刷新后当前会话历史保留;若最后一条是未完成的 user/assistant,自动重发该用户消息(依赖后端 L2 缓存命中,understanding 近乎免费;生成意图会重新生成草稿,可接受)。
4. **草稿渲染(修正问题 A)**:前端识别 `draft_pending_confirmation` JSON 响应,渲染草稿预览图 + 确认/拒绝按钮;对接 `/admin/draft/{id}/preview`、`/admin/draft/{id}/confirm`、`/admin/draft/{id}/reject`。
5. 后端**零改动**(草稿工作流后端已就绪)。

### 非目标(YAGNI)

- 后端 AgentLoop / tool calling / HITL 管理工具(Phase 2,见 2026-07-05 spec)。
- 后端 SSE 断点续传 / 事件持久化(后端 `sse.py` 是 fire-and-forget;续传靠前端重发实现)。
- 草稿多 preview 选择(draft 仅取 `previews[0]`;多预览图 UI 推迟)。
- 视频草稿确认后的播放器增强(复用 v1 `MediaVideo`)。
- 会话重命名(标题自动取首条用户消息前 20 字)。
- 会话搜索/置顶/分组。
- 前端单元测试(MVP 用手动清单 + smoke 脚本)。

## 3. 后端契约(前端消费的事实依据,已实测)

### 3.1 `/v1/chat/completions`(`stream:true`)的三种响应形态

| 意图 | `content-type` | body 形态 | 前端处理 |
|---|---|---|---|
| `understanding` | `text/event-stream` | 多个 `chat.completion.chunk`,`data: [DONE]` 结尾 | 按 SSE 流式累加文本(同 v1) |
| `generation:image` / `generation:video` | **`application/json`** | 单个 JSON 信封 `{"data":{draft_id,preview_url,generation_params},"_meta":{"draft_pending_confirmation":true}}` | **非流式**:整体 JSON 解析,进入草稿渲染流程 |

**关键判别**:`createChatCompletionStream` 必须先看响应 `content-type`。是 `application/json` → 走草稿分支;是 `text/event-stream` → 走 SSE 分支。**不能再无条件 `res.body.getReader()`。**

### 3.2 草稿工作流 endpoints(均 `authenticate_admin`,前端 admin key 可用)

| Endpoint | 方法 | 入参 | 返回 | 用途 |
|---|---|---|---|---|
| `/admin/draft/{id}/preview` | GET | — | `{draft_id, preview_data_url: "data:image/png;base64,...", preview_count}` | 取草稿预览图(低分辨率) |
| `/admin/draft/{id}/confirm` | POST | — | `{draft_id, upscaled_url: "data:image/png;base64,...", target_resolution:[w,h], algorithm}` | 确认 → 高清放大 → 返回最终图 |
| `/admin/draft/{id}/reject` | POST | — | `{previous_draft_id, new_draft_id, attempt_number, max_attempts, preview_url: "/admin/draft/{new_id}/preview"}` | 拒绝 → 重新生成新草稿 |
| `/admin/draft/{id}` | GET | — | `{draft_id, status, preview_count, generation_params, attempt_number, max_attempts, expires_at}` | 查草稿状态(可选,用于刷新后判断草稿是否仍 pending) |

来源:`aigateway-api/src/aigateway_api/admin_routes.py:2643-2800`、`draft_routes.py`。

**注意**:
- `confirm`/`reject` 是耗时操作(超分放大 / 重新生成),前端需 loading 态 + 超时保护。
- 草稿有 TTL(`expires_at`,默认 24h),过期后 confirm/reject 返回 `draft_expired` 错误,前端需降级提示"草稿已过期,请重新生成"。
- `preview_data_url` / `upscaled_url` 都是 base64 data URL,体积可能数 MB;localStorage 存不下也不该存 —— 见 §6 持久化策略。

### 3.3 understanding 流的 `_meta.routed_to`

同 v1:每个 SSE chunk 可能带 `_meta.routed_to.{intent,model}`,前端用于路由 badge。草稿 JSON 响应的 `_meta` 只有 `draft_pending_confirmation`,**不含 routed_to** —— 草稿消息的 intent/model 从 `generation_params.media_type` 推断(image→`generation:image`,video→`generation:video`),model 显示 `draft` 或留空。

## 4. 方案选择

### 4.1 布局(问题 C1)

**采用**:移除 `Layout.tsx` 的 `isChat` 特判,聊天页与其他页面共用顶栏 + 侧栏。`Chat.tsx` 高度从 `100vh` 改为 `calc(100vh - var(--nav-height) - 24px)`(56px 顶栏 + padding),在主内容区内 flex 布局。

**否决**:"聊天页仍全屏但加返回按钮" —— 违背用户"能看见其他菜单"的明确要求。

### 4.2 多会话(问题 C2)

**采用**:`ChatSession` 数据模型 + 聊天页内常驻会话列表(~200px 二级列)。

```
[Layout 侧栏 240px] | [会话列表 200px] | [聊天主区 flex-1]
```

**否决**:
- 抽屉式隐藏列表 —— 切换会话多一步,违背"常驻"偏好。
- 顶部下拉选择器 —— 会话多时不好用,且无法显示 updatedAt。

### 4.3 刷新续传(问题 C3 / B)

**采用**:重发最后一条用户消息。
- `ChatPageMessage` 新增 `incomplete?: boolean`。流式中断(刷新/离开/abort)的助手消息标记 `incomplete:true`。
- hook mount 时检测 active 会话末尾:若是 `user` 消息(助手还没回)或 `assistant` 且 `incomplete` → 移除末尾的空/未完成助手消息(保留 user 消息)→ 自动 `send(user 内容)`。
- 用 `useRef` 防重复触发(只 mount 时一次)。
- understanding 意图重发会命中 L2 缓存(cache key v2 同 prompt),近乎瞬时且不重复计费;生成意图重发会重新生成草稿(可接受,草稿生成本就是幂等预览)。

**否决**:
- 后端 SSE 断点续传 —— 后端无事件持久化,改动大,明确非目标。
- 仅保留历史不重发 —— 用户明确要求"自动续传"。
- 标记中断+手动重试按钮 —— 用户已选"自动重发"。

### 4.4 草稿渲染(问题 A)

**采用**:在 `createChatCompletionStream` 层判别 `content-type`,返回**联合类型**;`useChat` 按类型分流。

```typescript
type ChatResponse =
  | { kind: 'stream'; body: ReadableStream<Uint8Array> }          // understanding
  | { kind: 'draft'; draftId: string; previewUrl: string; generationParams: object }  // generation
```

草稿响应不进 SSE 解析循环,直接构造一条 `role:assistant` 的草稿消息(`content` 留空,新增 `draft` 字段挂载 draftId/previewUrl/状态),UI 渲染预览图 + 确认/拒绝按钮。

**否决**:
- 让后端把草稿也包成 SSE chunk —— 违背"后端零改动"且草稿本就是一次性 JSON。
- 前端继续按 SSE 解析然后特殊处理 `[DONE]` —— JSON body 根本没有 `[DONE]`,解析逻辑无法复用。

## 5. 数据模型

### 5.1 类型变更(`control-panel/src/types.ts`)

```typescript
/** 聊天页单条消息(v2:加 incomplete + draft) */
export interface ChatPageMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  intent?: 'understanding' | 'generation:image' | 'generation:video' | null
  model?: string
  error?: boolean
  incomplete?: boolean        // 新增:流式中断标记,用于刷新续传判断
  draft?: ChatDraftState      // 新增:草稿消息的附加状态(仅 generation 意图助手消息)
  ts: number
}

/** 草稿状态(挂在助手消息上) */
export interface ChatDraftState {
  draftId: string
  previewUrl: string           // "/admin/draft/{id}/preview"
  mediaType: 'image' | 'video'
  status: 'pending' | 'confirming' | 'confirmed' | 'rejecting' | 'rejected' | 'expired' | 'error'
  previewDataUrl?: string      // 渲染时拉取的 base64 data URL(不持久化,见 §6)
  resultDataUrl?: string       // confirm 后的高清图 data URL(不持久化)
  errorMessage?: string
}

/** 聊天会话 */
export interface ChatSession {
  id: string
  title: string                // 首条用户消息前 20 字,或"新对话"
  messages: ChatPageMessage[]
  createdAt: number
  updatedAt: number
}
```

### 5.2 localStorage 结构变更

| v1 | v2 |
|---|---|
| `aigateway:chat:messages` = `ChatPageMessage[]` | `aigateway:chat:sessions` = `ChatSession[]` |
| — | `aigateway:chat:active` = `string`(当前会话 id) |

**迁移**:hook 初始化时若检测到旧 `aigateway:chat:messages` 且无 `sessions`,迁移为单会话(`id: 'migrated'`,title 取首条 user 消息前 20 字),写入 `sessions` + `active`,然后删除旧 key。一次性。

## 6. 持久化策略(关键:不存 data URL)

base64 图片 data URL 体积可达数 MB,localStorage 单 key 5MB 上限会爆。策略:

- **持久化的**:`ChatSession[]` 中每条消息的 `content`(文本)、`intent`、`model`、`incomplete`、`draft.draftId`、`draft.previewUrl`、`draft.mediaType`、`draft.status`。
- **不持久化的**(`draft.previewDataUrl` / `draft.resultDataUrl`):base64 data URL。这些在渲染时从 `/admin/draft/{id}/preview` 或 confirm 响应**懒加载**,session 加载后若草稿消息缺 data URL 且 status 仍 pending/confirmed,重新拉取。
- **过期处理**:刷新后若 `GET /admin/draft/{id}` 返回 404/expired → `draft.status='expired'`,UI 显示"草稿已过期"。

## 7. 组件结构

```
pages/Chat.tsx                      顶层,布局 + 状态聚合
├── components/chat/SessionList.tsx        新增:会话列表 + 新建按钮
├── components/chat/ChatTimeline.tsx       改:渲染 draft 消息
├── components/chat/MessageBubble.tsx      改:分支渲染 draft / text
├── components/chat/DraftCard.tsx          新增:草稿预览图 + 确认/拒绝按钮 + loading
├── components/chat/ChatComposer.tsx       不变
├── components/chat/MediaImage.tsx         复用(渲染 data URL)
├── components/chat/MediaVideo.tsx         复用
├── components/chat/RoutingBadge.tsx       不变
└── hooks/
    useChatSessions.ts               新增:替代 useChat,管理多会话 + CRUD + 续传
    useChat.ts                       废弃(逻辑迁入 useChatSessions)
```

`api/client.ts` 新增:
- `createChatCompletion(body, signal): Promise<ChatResponse>` —— 替代 `createChatCompletionStream`,内部按 `content-type` 分流返回联合类型。
- `getDraftPreview(draftId): Promise<{previewDataUrl, previewCount}>`
- `confirmDraft(draftId): Promise<{upscaledUrl, targetResolution, algorithm}>`
- `rejectDraft(draftId): Promise<{newDraftId, previewUrl, attemptNumber, maxAttempts}>`
- `getDraftStatus(draftId): Promise<{status, expiresAt, ...}>`(刷新后判断过期)

## 8. 核心流程

### 8.1 发送消息(useChatSessions.send)

1. 构造 user 消息 + 空 assistant 占位,追加到 active session。
2. 调 `createChatCompletion({model:'auto', messages, stream:true})`。
3. 按 `ChatResponse.kind` 分流:
   - **`stream`**(understanding):`reader.read()` 按 SSE 帧累加 `content`,捕获 `_meta.routed_to`。流结束 → assistant 占位填充完成。中断 → 标记 `incomplete:true`。
   - **`draft`**(generation):不读流。把 assistant 占位转为草稿消息(`draft.status='pending'`,`draftId`/`previewUrl`/`mediaType` 来自响应),立即调 `getDraftPreview(draftId)` 拉预览图填 `previewDataUrl`。拉取失败 → `draft.status='error'`。
4. debounce 500ms 写 `sessions` 到 localStorage(不含 data URL)。

### 8.2 草稿确认(DraftCard.onConfirm)

1. `draft.status='confirming'`(按钮 loading)。
2. `confirmDraft(draftId)` → 成功:`draft.status='confirmed'`,`draft.resultDataUrl=upscaledUrl`,UI 渲染高清图替换预览。
3. 失败(`draft_expired` / `draft_confirm_failed`):`draft.status='expired'`/`'error'`,显示错误 + "重新生成"按钮(重新 `send` 原 prompt)。

### 8.3 草稿拒绝(DraftCard.onReject)

1. `draft.status='rejecting'`。
2. `rejectDraft(draftId)` → 返回新 `preview_url`(新 draftId)。把当前草稿消息的 `draftId`/`previewUrl` 更新为新值,`status='pending'`,重新拉预览图。
3. 失败:同确认的降级。

### 8.4 刷新续传(useChatSessions mount)

```
on mount:
  sessions = loadSessions() or migrate()
  activeId = loadActive() or sessions[0]?.id
  if active session 末尾消息:
    case (user 消息):                      // 助手还没回就刷新了
      send(末尾 user.content)              // 内部会先移除该 user 消息再重发,避免重复
    case (assistant 且 incomplete):
      移除该 assistant 占位
      send(其前一条 user.content)
    case (assistant 且完整 或 draft 且非 pending):
      不动
  ref 已标记 → 防重复
```

**草稿消息的续传判断**:若末尾是 `draft.status='pending'` 的草稿消息,视为"已完成"(草稿已生成,只是没确认)→ 不重发。刷新后懒加载重新拉预览图即可。若 `draft.status` 是 `'confirming'`/`'rejecting'`(刷新时正在请求),降级为 `'pending'` 让用户重新点确认/拒绝(草稿 confirm 非幂等,不能自动重试)。

## 9. UI 布局细节

```
┌─────────────────────────────────────────────────────────┐
│ 顶栏(56px,Layout 共用)                                 │
├──────────┬──────────────┬──────────────────────────────┤
│ Layout   │ 会话列表      │ 聊天主区                      │
│ 侧栏     │ (200px)       │ (flex-1)                     │
│ (240px)  │ + 新对话      │ ┌──────────────────────────┐ │
│          │ ─ sess 1 (活) │ │ Timeline                 │ │
│ 概览     │   sess 2      │ │  user: 画一只橘猫        │ │
│ 聊天     │   sess 3 🗑   │ │  assistant: [DraftCard]  │ │
│ 模型配置 │              │ │   🖼️ 预览图              │ │
│ ...      │              │ │   [✓ 确认] [✗ 重新生成]   │ │
│          │              │ └──────────────────────────┘ │
│          │              │ [Composer]                   │
└──────────┴──────────────┴──────────────────────────────┘
```

- 会话列表项:title(截断)+ 相对时间(刚刚/5分钟前)。hover 显示删除 🗑。active 项高亮。
- "新对话"按钮在列表顶部,点击创建空 session 并切 active。
- 无 session 时列表显示"点击新建对话开始"。
- 草稿预览图 maxWidth 100%,确认后高清图替换,带"已确认 · {algorithm} · {WxH}"小标。

## 10. 错误处理

| 场景 | 处理 |
|---|---|
| `createChatCompletion` HTTP 非 2xx | `useChat.send` catch → setError,移除空 assistant 占位 |
| 响应 `content-type` 既非 json 也非 event-stream | 当作错误,提示"未知响应类型" |
| understanding 流中途网络断 | assistant 占位标记 `incomplete:true`,保留已收 content,UI 显示"(已中断)"小标 |
| `getDraftPreview` 404/expired | `draft.status='expired'`,UI 显示"草稿已过期" + 重新生成按钮 |
| `confirmDraft` 超时(>60s) | abort,`draft.status='error'`,提示"放大超时,可重试" |
| `rejectDraft` 失败 | `draft.status='error'`,保留原草稿,提示重试 |
| localStorage 写入超限(quota) | 静默捕获(console.warn),不阻塞会话;旧会话可手动删除腾空间 |
| 旧 key 迁移失败(JSON 损坏) | 丢弃旧数据,初始化空 sessions,不抛错 |

## 11. 测试策略(手动清单 + smoke)

无前端单测(MVP 非目标)。验证清单:

1. **布局**:进 /chat,顶栏+侧栏可见,聊天区在右侧不溢出。
2. **多会话**:新建对话 → 发消息 → 切到另一会话 → 切回,消息隔离正确。
3. **持久化**:发消息后刷新 → sessions 全部保留,active 正确。
4. **续传-文本**:发 understanding 请求,流式中刷新 → 自动重发,内容恢复(命中缓存)。
5. **续传-草稿**:发图片请求得到草稿后刷新 → 草稿消息保留,预览图重新加载,可确认。
6. **草稿确认**:点确认 → loading → 高清图替换。
7. **草稿拒绝**:点重新生成 → 新预览图。
8. **草稿过期**:手动改 draftId 刷新 → 显示过期 + 重新生成。
9. **删除会话**:删除非 active 会话 → 列表更新;删除 active → 切到第一个。
10. **迁移**:清 localStorage,写旧 `aigateway:chat:messages`,刷新 → 迁移为单会话。

smoke 脚本:复用 v1 的 `tests/ui/` 模式(若存在),加一条"图片草稿渲染"用例。

## 12. 实现顺序(给 writing-plans 的输入)

1. types.ts:加 `incomplete` / `ChatDraftState` / `ChatSession`。
2. api/client.ts:`createChatCompletion`(联合类型)+ 4 个 draft endpoint 函数。
3. hooks/useChatSessions.ts:多会话状态机 + 迁移 + 续传。
4. components/chat/DraftCard.tsx:草稿 UI。
5. components/chat/MessageBubble.tsx:分支渲染 draft。
6. components/chat/SessionList.tsx:会话列表。
7. pages/Chat.tsx:新布局 + 组装。
8. components/Layout.tsx:移除 isChat 特判。
9. 删除 hooks/useChat.ts(逻辑已迁)。
10. 手动验证清单 + smoke。
