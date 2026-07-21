# 控制台聊天窗 MVP 设计

**Status:** Draft
**Date:** 2026-07-18
**Author:** Gateway2 team via brainstorming skill
**Related:** `docs/superpowers/specs/2026-07-05-control-panel-chat-agent-design.md`(Entry B 完整形态,本 spec 是其前端先行子集)

---

## 1. 背景与定位

`docs/superpowers/specs/2026-07-05-control-panel-chat-agent-design.md` 描绘了 Entry B 的完整形态:控制台 `/chat` 页面 + `/admin/agent/chat` SSE 后端 + AgentLoop(tool calling + HITL)+ 9 个管理工具。但该 spec **目前一行代码都没实现**(无 `agent/` 目录、无 `agent_routes.py`、无 `Chat.tsx`)。

本 spec 是 Entry B 的**前端先行 MVP 子集**:

- **做**:控制台新增 `/chat` 页面,复用现有 `/v1/chat/completions` 出口,支持文本多轮对话 + 图片/视频生成渲染。
- **不做(明确推迟到后续阶段)**:后端 AgentLoop、tool calling、HITL 确认卡、管理工具、`/admin/agent/chat` 后端、`admin_service.py` 抽取。这些留在 2026-07-05 spec 里,未来作为 Phase 2 落地。

**后端零改动** —— 本 spec 纯前端。后端 `classify_request`(LLM 意图预测)+ LiteLLMBridge auto resolver + `_do_image_generation` / `_do_video_generation` 已全部就绪,前端直接消费其 SSE 输出。

## 2. 目标与非目标

### 目标

1. 控制台侧栏新增"聊天"入口,路由 `/chat`,全屏聊天页。
2. 文本多轮对话:每次发送全量历史给 `/v1/chat/completions`,后端 `classify_request` + cache key v2 自然受益。
3. 图片生成渲染:意图为 `generation:image` 时,流式 `done` 后渲染 `<img>`。
4. 视频生成渲染:意图为 `generation:video` 时,解析返回的 job id,前端轮询 `GET /v1/videos/{id}` 到出结果,渲染 `<video>`。
5. 路由透明度:每条助手消息显示意图 badge(🧠/🎨/🎬)+ 后端实际选中的模型名(从 `_meta.routed_to` 读回)。
6. 历史持久化:localStorage 单会话,按消息变更 debounce 写入,刷新可重现。
7. 复用现有认证:localStorage `aigateway_api_key`,不单独做登录页。

### 非目标(YAGNI)

- 后端 AgentLoop / tool calling / HITL / 管理工具(Phase 2)
- `/admin/agent/chat` 后端
- SSE 事件重发 / 断点续传(断连即取消,与后端 `sse.py` 行为一致)
- 多会话 / 会话列表
- 前端 model 选择器(请求固定 `model: "auto"`,智能路由完全在后端)
- 前端单元测试(MVP 用手动清单 + smoke 脚本)
- 后端改动 / 后端测试(零改动)

## 3. 后端响应契约(前端消费的事实依据)

`POST /v1/chat/completions`(`stream: true`)对三种意图的 SSE 输出形态(来源:`aigateway-core/src/aigateway_core/route/bridge/litellm_bridge.py:1249-1318`、`route/streaming/sse.py:38-48`):

| 意图(`_meta.routed_to.intent`) | SSE chunk 形态 | `choices[0].delta.content` |
|---|---|---|
| `understanding` | 多个流式 chunk,逐 token | 流式文本片段 |
| `generation:image` | **单个** chunk(`finish_reason:"stop"`) | 裸图片 URL 或 base64 字符串(无 markdown 包裹、无结构化字段) |
| `generation:video` | **单个** chunk | `"Video generation submitted. id=<vid>, poll /v1/videos/<vid>"`(不阻塞,异步) |

**所有 chunk** 为标准 `chat.completion.chunk` 形态,`_meta` 直接挂在 chunk 上,含 `routed_to.intent` / `routed_to.model` / `cost`。流以 `data: [DONE]` 终止。失败时发单个 error chunk,`choices[0].delta.content = "[Image generation error] ..."` / `"[Video generation error] ..."` 且带 `error` 字段。

**视频异步**:视频不阻塞,返回 job id 嵌在 content 文本里(无结构化字段)。前端正则 `id=([\w-]+)` 解析后轮询 `GET /v1/videos/{id}`(后端 `retrieve_video` 已存在,`litellm_bridge.py:905-920`)。

**注意(非阻塞 caveat)**:非流式响应外层是 `{"data": {...}, "message":"success", "_meta":{...}}` 信封,choices 嵌在 `data` 下;但本 spec 全程用流式,流式 chunk 本身是标准形态,不受信封影响。

## 4. 方案选择

**方案 A(选定):流式统一入口,前端按 content 形态分支渲染。**

所有请求走 `createChatCompletionStream`(已存在于 `api/client.ts:104`)。前端维护消息渲染器,收到完整 assistant 消息后判断 content 形态:URL/b64 → 图片;匹配 `id=` → 视频;否则文本。路由 badge 从首个带 `_meta` 的 chunk 取。

被否决的方案:
- **B(流式文本 + 非流式多媒体)**:多媒体用非流式 `createChatCompletion` 拿完整 JSON。否决理由:前端要么重复实现意图分类(与后端 `classify_request` 重复),要么发两次请求试探意图;后端已统一用 SSE chunk 返回多媒体,无需分裂。
- **C(纯文本 MVP)**:不处理多媒体。否决理由:用户明确选择"文本+多媒体生成"。

**判定规则(双保险,任一命中即按对应类型渲染):**
- **图片**:`intent === "generation:image"` **或** content 匹配 `^https?://` / `^data:image/` / base64 长串
- **视频**:`intent === "generation:video"` **或** content 匹配 `id=([\w-]+)`
- **文本**:其余(含理解意图、空 content、`[Image generation error]` 类错误文本走错误样式)

双保险意义:即便后端某天 content 格式微调,`_meta.intent` 仍能兜住;反之亦然。

## 5. 前端架构与数据流

### 5.1 文件结构

```
control-panel/src/
  pages/Chat.tsx                      # /chat 页面壳:布局 + 状态编排
  components/chat/
    ChatTimeline.tsx                  # 消息流(用户/助手/图/视频/错误)
    ChatComposer.tsx                  # textarea + 发送/停止
    MessageBubble.tsx                 # 单条消息渲染分发器
    MediaImage.tsx                    # content 是 URL/b64 → <img>(done 后渲染)
    MediaVideo.tsx                    # 解析 id → 轮询 /v1/videos/{id} → <video>
    RoutingBadge.tsx                  # _meta.routed_to.intent → 🧠/🎨/🎬 + 模型名
  hooks/
    useChat.ts                        # 状态机:messages/streaming/error,封装 SSE
  api/client.ts                       # 已有 createChatCompletionStream,新增 getVideoStatus
  App.tsx                             # 新增 /chat 路由
  components/Layout.tsx               # 侧栏新增"聊天"入口
```

### 5.2 数据流(一次对话)

```
用户输入 ─→ useChat.send(text)
  ① 追加 user message 到 messages
  ② 追加空 assistant message(占位),streaming=true
  ③ createChatCompletionStream({ messages: 全量历史, model:"auto", stream:true })
     ▼
SSE chunk 流入(useChat 内 ReadableStream reader 解析 "data: ...\n\n"):
  ├─ 首个带 _meta 的 chunk → 抽 routed_to.intent/model 存到当前 assistant msg
  ├─ delta.content 累加到 assistant msg.content(streaming 中实时刷新)
  ├─ error chunk → 标 msg.error=true
  └─ [DONE] → streaming=false
     ▼
渲染阶段(MessageBubble 收到完整 content 后判定):
  ├─ image  → <MediaImage content={msg.content} done={!streaming} />
  ├─ video  → <MediaVideo content={msg.content} />
  ├─ error  → 错误气泡(红边)
  └─ text   → 文本气泡(markdown)
     ▼
持久化:messages 变更 → debounce ~500ms 写 localStorage
```

**消息发送体**:全量历史(`messages.map(m => ({role, content}))`)+ `model: "auto"` + `stream: true`。多轮上下文靠每次发全量历史实现。

## 6. 组件与状态细节

### 6.1 `useChat` hook

```typescript
type Role = 'user' | 'assistant'
type Intent = 'understanding' | 'generation:image' | 'generation:video' | null

interface ChatMessage {
  id: string
  role: Role
  content: string          // 流式累加;助手最终态可能是 URL / id 文本
  intent?: Intent          // 首个带 _meta 的 chunk 抽出
  model?: string           // _meta.routed_to.model(读回,非用户提供)
  error?: boolean          // 错误 chunk 标记
  ts: number
}

interface UseChat {
  messages: ChatMessage[]
  streaming: boolean
  send: (text: string) => Promise<void>
  stop: () => void           // AbortController.abort,断流
  clear: () => void          // 清空 messages + 删 localStorage
  error: string | null       // 连接级错误(非单条消息错误)
}
```

SSE 解析:在 `createChatCompletionStream` 返回的 `ReadableStream` 上用 `TextDecoder` 逐块读,按 `\n\n` 分帧,每帧剥 `data: ` 前缀后 `JSON.parse`;遇 `[DONE]` 结束;遇 chunk 带 `error` 字段 → 标当前消息 `error=true`。`AbortController` 挂在 hook ref 上,`stop()` 调 `abort()` —— 后端 SSE 断连即取消(`sse.py` 已支持),不会 hang。

### 6.2 `MessageBubble` —— 渲染分发器

```tsx
function MessageBubble({ msg, isStreaming }: { msg: ChatMessage; isStreaming: boolean }) {
  // isStreaming = "本条消息是否仍在流式接收"(仅最后一条活跃助手消息为 true,历史消息恒 false)
  if (msg.role === 'user') return <UserBubble>{msg.content}</UserBubble>

  const kind = classifyContent(msg.intent, msg.content)  // 'image' | 'video' | 'text'
  return (
    <AssistantBubble error={msg.error}>
      {msg.model && <RoutingBadge intent={msg.intent} model={msg.model} />}
      {kind === 'image' && <MediaImage content={msg.content} done={!isStreaming} />}
      {kind === 'video' && <MediaVideo content={msg.content} done={!isStreaming} />}
      {kind === 'text'  && <TextContent text={msg.content} error={msg.error} />}
    </AssistantBubble>
  )
}
```

`classifyContent(intent, content)` 即 §4 双保险判定。**关键取舍**:图片/视频在流式中途 content 是半截 URL / 半截 id,直接渲染会闪 `<img src="http://...">`。因此:
- `MediaImage` 在 `!done` 时显示 loading 占位,`done` 后才渲染 `<img>`。
- `MediaVideo` 等 `done`(id 完整)后才开始轮询。
- 文本则实时流式追加(不挡)。

### 6.3 `MediaVideo` —— 视频轮询组件

接收 `done` prop(`MessageBubble` 传 `!isStreaming`);`done=false` 时显示 loading 占位,`done=true` 后开始轮询:

```
done=true 后:
  ① 正则 content 提取 videoId = /id=([\w-]+)/
  ② 状态机:polling(每 ~3s GET /v1/videos/{id})
       → succeeded:取 video url → <video controls>
       → failed:   错误文案 + 手动重试按钮
       → 超时(~120s):超时文案 + 重试按钮
  ③ 组件卸载 → 清 interval,不泄漏
```

`getVideoStatus(id)` 新增到 `api/client.ts`:GET `/v1/videos/{id}`,返回字段按后端 `retrieve_video` 实际返回定(spec 不锁死字段名,实现时核对)。轮询只在视频消息可见时跑;切走页面卸载即停,回来重新挂载重新轮询(id 仍在 message 里)。刷新页面后,历史视频消息从 localStorage 恢复 content,重新挂载续轮询 —— **持久化的是 id 而非 URL**(URL 有时效会过期,id 能重新拿到结果)。

### 6.4 `ChatComposer`

textarea(Enter 发送 / Shift+Enter 换行)+ 发送按钮;`streaming` 时发送按钮变"停止"调 `stop()`。无 model 选择器(请求固定 `model: "auto"`)。

### 6.5 `RoutingBadge`

`🧠 理解` / `🎨 图片` / `🎬 视频` + 模型名小字,从 `msg.intent` + `msg.model` 渲染;`intent === null` 时不显示。模型名是后端智能路由结果的反馈(从 `_meta.routed_to.model` 读回),非用户输入。

## 7. localStorage

```
aigateway:chat:messages        # ChatMessage[] —— 全量历史(含 intent/model/error/ts)
```

- **key 不绑 `login_key_id`**:MVP 单会话,登录 key 本身已在 `aigateway_api_key`。未来做多会话/多用户隔离再加 `:{key_id}` 后缀。
- **写入节流**:每次 `messages` 变更后 debounce ~500ms 写一次,避免流式每 token 写盘。
- **`clear()`**:删 key + 清内存。
- **读取**:页面挂载时一次性读;迁移旧格式失败当空。
- **视频消息持久化**:存原始 content(`"Video generation submitted. id=xxx, ..."`) + `intent`,不存视频 URL。

## 8. 错误处理

| 场景 | 处理 |
|---|---|
| 未设 API key(localStorage 无 `aigateway_api_key`) | Chat 页面空状态卡片:"请先在任一页面设置 API Key",附跳转提示(与其他页面隐式 auth 一致,不单独做登录页) |
| 流式中途网络断 / fetch 抛错 | 当前 assistant 消息标 `error`,`useChat.error` 设提示;保留已累加 content;可重发 |
| 后端 SSE error chunk(`[Image generation error] ...` / `[Video generation error] ...`) | 消息标 `error=true`,文本走错误样式(红边),不中断会话 |
| 视频轮询失败 / 超时 | `MediaVideo` 内部状态,显示"生成失败/超时"+ 手动重试按钮,不影响会话主流程 |
| HTTP 4xx(配额超限 / 认证失效) | `createChatCompletionStream` 已 throw(带 message);`useChat.send` catch → `error` 提示;保留 user 消息,assistant 占位消息移除 |
| `stop()` 中断 | assistant 消息保留已收部分 content,标 `error=false`(正常截断);流式停止 |

## 9. 测试策略

后端零改动,不加后端测试。前端 MVP 用手动清单 + smoke 脚本。

### 9.1 前端手动清单

- [ ] 未设 key 打开 `/chat` → 空状态提示
- [ ] 发"你好"→ 文本流式追加,带 🧠 理解 badge + 模型名
- [ ] 多轮:第二句能引用第一轮上下文
- [ ] 发"画一只猫"→ 🎨 图片 badge,流式 `done` 后渲染 `<img>`(中途不闪半截图)
- [ ] 发"生成一段视频"→ 🎬 视频 badge,显示轮询中,完成后渲染 `<video controls>`
- [ ] 视频轮询超时 → 超时文案 + 重试按钮
- [ ] 流式中点停止 → 保留已收文本,可继续发下一条
- [ ] 断网模拟 → 错误提示,重发可用
- [ ] 刷新页面 → 历史消息重现;视频消息重新轮询
- [ ] 点清空 → 历史清空 + localStorage 删
- [ ] 侧栏出现"聊天"入口,路由 `/chat` 可达

### 9.2 Smoke 脚本(`scripts/smoke_chat.sh`,可选)

`docker compose up -d` 后用 admin key `curl -N /v1/chat/completions` 发文本和图片请求,断言 SSE 出 `delta.content` 且图片请求 content 是 URL。属后端烟测,顺带验证前端要消费的形态没变。

### 9.3 运行命令

```bash
cd control-panel && npm run dev    # Vite :5173,代理 /aigateway/* → :8000
npm run build                      # tsc -b && vite build
```

## 10. 实施顺序建议(给 writing-plans skill)

1. **路由与入口**:`App.tsx` 加 `/chat` 路由,`Layout.tsx` 侧栏加"聊天"入口,空 `Chat.tsx` 页面壳。
2. **SSE 客户端**:确认 `createChatCompletionStream` 够用,新增 `getVideoStatus`。
3. **`useChat` hook**:状态机 + SSE 解析 + AbortController + localStorage 读写(debounce)。
4. **基础渲染**:`ChatTimeline` + `MessageBubble` + `ChatComposer`,先跑通文本流式。
5. **多媒体**:`MediaImage`(done 后渲染)+ `RoutingBadge`。
6. **视频轮询**:`MediaVideo` 状态机 + 卸载清理。
7. **错误处理**:各错误分支接齐。
8. **手动清单验证** + smoke 脚本。
9. **文档**:更新 `CLAUDE.md`("Dead frontend code"条目:`createChatCompletionStream` 不再 reserved,已在 chat 页用)。

## 11. Out of scope(明确不做)

- 后端 AgentLoop / tool calling / HITL / 9 个管理工具(Phase 2,见 2026-07-05 spec)
- `/admin/agent/chat` 后端、`admin_service.py` 抽取、KeyStore `is_admin` 相关前端
- SSE 事件重发 / 断点续传
- 多会话 / 会话列表 / 前端 model 选择器
- 前端单元测试(Vitest 后续引入)
- 后端改动 / 后端测试
