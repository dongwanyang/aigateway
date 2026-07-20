# 控制台聊天窗 MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在控制台新增全屏 `/chat` 聊天页,复用现有 `/v1/chat/completions` SSE 出口,支持文本多轮对话 + 图片/视频生成渲染,历史存 localStorage。

**Architecture:** 纯前端,后端零改动。所有请求走 `createChatCompletionStream`(已存在),前端 `useChat` hook 用 `ReadableStream` reader 解析 SSE 帧,按 `_meta.routed_to.intent` + content 形态双保险判定渲染文本/图片/视频。视频异步:解析 content 里的 `id=`,前端轮询 `GET /v1/videos/{id}`。

**Tech Stack:** React + TypeScript + Vite + Tailwind + lucide-react + react-router-dom。复用现有 CSS 变量(`--color-*`)、`Card` 组件、`useTheme`。

## Global Constraints

- 后端零改动:本计划不碰 `aigateway-api/` / `aigateway-core/` 任何文件。
- 请求固定 `model: "auto"`(智能路由在后端 `classify_request` + LiteLLMBridge auto resolver)。前端不提供模型选择器。
- 流式响应形态:`chat.completion.chunk` 带 `_meta.routed_to.intent` / `.model`;图片/视频意图只发单个 chunk(`finish_reason:"stop"`)+ `data: [DONE]`。
- 视频响应 content 形如 `"Video generation submitted. id=<vid>, poll /v1/videos/<vid>"`,前端正则 `/id=([\w-]+)/` 解析后轮询 `GET /v1/videos/{id}`(后端 `video_routes.py` 已存在,passthrough 上游 JSON,字段 `{id, status, video?: {url}, error?: {...}}`)。
- 图片 content 是裸 URL 或 base64 字符串(无 markdown 包裹)。
- 类型名避撞:types.ts 已有 `ChatMessage`(OpenAI wire 类型),聊天页本地消息类型用 `ChatPageMessage`。
- localStorage key:`aigateway:chat:messages`(单会话,不绑 login key)。
- 认证:复用 `ensureAuthHeaders()`(localStorage `aigateway_api_key`),不单独做登录页。
- 不做:后端 AgentLoop / tool calling / HITL / 管理工具 / SSE 重发 / 多会话 / 前端单测(MVP 手动清单 + smoke 脚本)。
- 工作流约束(CLAUDE.md Rule 0):改完跑 `window-code-review` skill;**不自动 commit**,等用户批准。

---

## File Structure

```
control-panel/src/
  api/client.ts            [修改] 新增 getVideoStatus
  types.ts                 [修改] 新增 ChatPageMessage / VideoStatus 相关类型
  hooks/useChat.ts         [新建] 状态机 + SSE 解析 + localStorage
  components/chat/         [新建目录]
    ChatTimeline.tsx       [新建] 消息流
    ChatComposer.tsx       [新建] 输入框 + 发送/停止
    MessageBubble.tsx      [新建] 渲染分发器 + classifyContent
    MediaImage.tsx         [新建] done 后渲染 <img>
    MediaVideo.tsx         [新建] 轮询 + <video>
    RoutingBadge.tsx       [新建] 🧠/🎨/🎬 + 模型名
  pages/Chat.tsx           [新建] /chat 页面壳
  App.tsx                  [修改] 新增 /chat 路由
  components/Layout.tsx    [修改] 侧栏加"聊天"入口
scripts/smoke_chat.sh      [新建] 可选 smoke 脚本
```

每个文件单一职责:`useChat` 只管状态 + SSE + 持久化;`MessageBubble` 只管分发渲染;`MediaImage`/`MediaVideo` 各管一种媒体;`Chat.tsx` 是布局壳。

---

## Task 1: 路由与侧栏入口 + 空 Chat 页面

**Files:**
- Create: `control-panel/src/pages/Chat.tsx`
- Modify: `control-panel/src/App.tsx`
- Modify: `control-panel/src/components/Layout.tsx`(navItems 第 5-14 行)

**Interfaces:**
- Produces: `Chat` 默认导出组件(后续 task 填充内容)。

- [ ] **Step 1: 写空 Chat 页面壳**

Create `control-panel/src/pages/Chat.tsx`:

```tsx
import Card from '@/components/Card'

export default function Chat() {
  return (
    <Card title="聊天">
      <p style={{ color: 'var(--color-text-secondary)' }}>聊天功能建设中…</p>
    </Card>
  )
}
```

- [ ] **Step 2: App.tsx 加路由**

Modify `control-panel/src/App.tsx`:

在 import 区(第 10 行 `import Config from '@/pages/Config'` 后)加:

```tsx
import Chat from '@/pages/Chat'
```

在 `<Routes>` 内 `/config` 路由后(第 31 行后)加:

```tsx
            <Route path="/chat" element={<PageErrorBoundary><Chat /></PageErrorBoundary>} />
```

- [ ] **Step 3: Layout.tsx 侧栏加入口**

Modify `control-panel/src/components/Layout.tsx` 第 1 行 import,加 `MessageSquare` 图标:

```tsx
import { LayoutDashboard, Puzzle, DollarSign, Shield, Database, FileText, Sun, Moon, BookOpen, Settings, Bot, MessageSquare } from 'lucide-react'
```

在 navItems 数组(`'概览'` 那条后,即第 6 行后)加:

```tsx
  { path: '/chat', label: '聊天', icon: MessageSquare },
```

- [ ] **Step 4: 验证构建**

Run: `cd control-panel && npx tsc -b --noEmit`
Expected: 无错误退出码 0。

- [ ] **Step 5: 手动验证**

Run: `cd control-panel && npm run dev`
打开浏览器 http://localhost:5173/chat → 看到"聊天功能建设中…"卡片;侧栏出现"聊天"入口且高亮。Ctrl-C 停止 dev server。

- [ ] **Step 6: Commit**

```bash
git add control-panel/src/pages/Chat.tsx control-panel/src/App.tsx control-panel/src/components/Layout.tsx
git commit -m "feat(chat): add /chat route and sidebar entry with placeholder page"
```

---

## Task 2: types.ts 加聊天页类型

**Files:**
- Modify: `control-panel/src/types.ts`(末尾追加)

**Interfaces:**
- Produces: `ChatPageMessage`、`VideoStatus`、`VideoStatusResponse` 类型,供 `useChat` / `MessageBubble` / `MediaVideo` 使用。

- [ ] **Step 1: 追加类型定义**

在 `control-panel/src/types.ts` 末尾追加:

```typescript
// ------------------------------------------------------------------
// Chat 页面本地类型(聊天窗 MVP)
// ------------------------------------------------------------------

/** 聊天页单条消息(区别于 OpenAI wire 类型 ChatMessage) */
export interface ChatPageMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  intent?: 'understanding' | 'generation:image' | 'generation:video' | null
  model?: string
  error?: boolean
  ts: number
}

/** GET /v1/videos/{id} 返回的上游视频任务状态(passthrough) */
export interface VideoStatusResponse {
  id?: string
  status?: string  // 'queued' | 'in_progress' | 'succeeded' | 'failed' | ...
  video?: { url?: string }
  error?: { code?: string; message?: string }
}
```

- [ ] **Step 2: 验证类型编译**

Run: `cd control-panel && npx tsc -b --noEmit`
Expected: 退出码 0。

- [ ] **Step 3: Commit**

```bash
git add control-panel/src/types.ts
git commit -m "feat(chat): add ChatPageMessage and VideoStatus types"
```

---

## Task 3: api/client.ts 加 getVideoStatus

**Files:**
- Modify: `control-panel/src/api/client.ts`(在 `createChatCompletionStream` 后,约第 127 行)

**Interfaces:**
- Consumes: `ensureAuthHeaders`(已存在 client.ts:31)、`fetchJson` 模式。
- Produces: `getVideoStatus(videoId: string): Promise<VideoStatusResponse>`。

- [ ] **Step 1: 加 import 与函数**

在 `control-panel/src/api/client.ts` 顶部 type import 块(第 10-25 行附近,`import type { ... } from '@/types'`)里加 `VideoStatusResponse`:

```typescript
  VideoStatusResponse,
```

在 `createChatCompletionStream` 函数后(第 127 行 `}` 后)加:

```typescript
/** GET /v1/videos/{id} —— 轮询视频生成任务状态(passthrough 上游 JSON)。 */
export async function getVideoStatus(videoId: string): Promise<VideoStatusResponse> {
  const headers = await ensureAuthHeaders()
  const res = await fetch(`${API_BASE}/v1/videos/${encodeURIComponent(videoId)}`, {
    headers,
  })
  if (!res.ok) {
    throw new Error(`视频状态查询失败: HTTP ${res.status}`)
  }
  return (await res.json()) as VideoStatusResponse
}
```

- [ ] **Step 2: 验证编译**

Run: `cd control-panel && npx tsc -b --noEmit`
Expected: 退出码 0。

- [ ] **Step 3: Commit**

```bash
git add control-panel/src/api/client.ts
git commit -m "feat(chat): add getVideoStatus polling client"
```

---

## Task 4: useChat hook(状态机 + SSE 解析 + localStorage)

**Files:**
- Create: `control-panel/src/hooks/useChat.ts`

**Interfaces:**
- Consumes: `createChatCompletionStream`(`api/client.ts:104`)、`ChatCompletionRequest`/`ChatPageMessage`(types.ts)、`ChatMessage`(wire 类型)。
- Produces: `useChat()` hook,返回 `{ messages, streaming, send, stop, clear, error }`。

- [ ] **Step 1: 写 useChat hook**

Create `control-panel/src/hooks/useChat.ts`:

```typescript
import { useCallback, useEffect, useRef, useState } from 'react'
import { createChatCompletionStream } from '@/api/client'
import type { ChatPageMessage, ChatMessage } from '@/types'

const STORAGE_KEY = 'aigateway:chat:messages'

function loadMessages(): ChatPageMessage[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed as ChatPageMessage[]
  } catch {
    return []
  }
}

let idCounter = 0
function nextId(): string {
  idCounter += 1
  return `msg-${Date.now()}-${idCounter}`
}

export interface UseChat {
  messages: ChatPageMessage[]
  streaming: boolean
  error: string | null
  send: (text: string) => Promise<void>
  stop: () => void
  clear: () => void
}

export function useChat(): UseChat {
  const [messages, setMessages] = useState<ChatPageMessage[]>(loadMessages)
  const [streaming, setStreaming] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const assistantIdRef = useRef<string | null>(null)

  // debounce 持久化:messages 变更后 500ms 写一次
  useEffect(() => {
    const t = setTimeout(() => {
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(messages))
      } catch {
        // quota / 序列化失败,静默忽略
      }
    }, 500)
    return () => clearTimeout(t)
  }, [messages])

  const stop = useCallback(() => {
    abortRef.current?.abort()
    abortRef.current = null
    setStreaming(false)
  }, [])

  const clear = useCallback(() => {
    stop()
    setMessages([])
    try {
      localStorage.removeItem(STORAGE_KEY)
    } catch {
      // 忽略
    }
  }, [stop])

  const send = useCallback(async (text: string) => {
    const trimmed = text.trim()
    if (!trimmed || streaming) return

    setError(null)

    const userMsg: ChatPageMessage = {
      id: nextId(), role: 'user', content: trimmed, ts: Date.now(),
    }
    const assistantId = nextId()
    assistantIdRef.current = assistantId
    const assistantMsg: ChatPageMessage = {
      id: assistantId, role: 'assistant', content: '', ts: Date.now(),
    }

    // 历史 wire 格式(发给后端)
    const wireMessages: ChatMessage[] = [...messages, userMsg].map(m => ({
      role: m.role, content: m.content,
    }))

    setMessages(prev => [...prev, userMsg, assistantMsg])
    setStreaming(true)

    const controller = new AbortController()
    abortRef.current = controller

    try {
      const stream = await createChatCompletionStream({
        model: 'auto', messages: wireMessages, stream: true,
      })
      const reader = stream.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        // 按 SSE 帧分隔 \n\n
        let idx: number
        while ((idx = buffer.indexOf('\n\n')) !== -1) {
          const frame = buffer.slice(0, idx)
          buffer = buffer.slice(idx + 2)
          const line = frame.trim()
          if (!line.startsWith('data:')) continue
          const payload = line.slice(5).trim()
          if (payload === '[DONE]') {
            setStreaming(false)
            abortRef.current = null
            return
          }
          try {
            const chunk = JSON.parse(payload)
            const delta = chunk?.choices?.[0]?.delta
            const meta = chunk?._meta?.routed_to
            const isErr = !!chunk?.error
            setMessages(prev => prev.map(m => {
              if (m.id !== assistantId) return m
              const next: ChatPageMessage = { ...m }
              if (delta?.content) next.content += delta.content
              if (meta?.intent && !next.intent) next.intent = meta.intent
              if (meta?.model && !next.model) next.model = meta.model
              if (isErr) next.error = true
              return next
            }))
          } catch {
            // 非 JSON 帧,跳过
          }
        }
      }
      // 流自然结束(未收到 [DONE])
      setStreaming(false)
    } catch (e) {
      if (controller.signal.aborted) {
        // 用户主动停止,保留已收内容
        setStreaming(false)
      } else {
        const msg = e instanceof Error ? e.message : '请求失败'
        setError(msg)
        // 移除空的占位助手消息
        setMessages(prev => prev.filter(m => !(m.id === assistantId && m.content === '')))
        setStreaming(false)
      }
    } finally {
      abortRef.current = null
    }
  }, [messages, streaming])

  return { messages, streaming, error, send, stop, clear }
}
```

- [ ] **Step 2: 验证编译**

Run: `cd control-panel && npx tsc -b --noEmit`
Expected: 退出码 0。

- [ ] **Step 3: Commit**

```bash
git add control-panel/src/hooks/useChat.ts
git commit -m "feat(chat): add useChat hook with SSE parsing and localStorage"
```

---

## Task 5: RoutingBadge + classifyContent + MessageBubble(文本先行)

**Files:**
- Create: `control-panel/src/components/chat/RoutingBadge.tsx`
- Create: `control-panel/src/components/chat/MessageBubble.tsx`

**Interfaces:**
- Consumes: `ChatPageMessage`(types.ts)。
- Produces: `MessageBubble` 组件、`classifyContent` 函数、`RoutingBadge` 组件。`MediaImage`/`MediaVideo` 在本 task 用占位(下两个 task 实现),`MessageBubble` 先引它们但本 task 不创建——改用条件分支占位。

**注意:** 本 task 不创建 `MediaImage`/`MediaVideo`,先在 `MessageBubble` 里对 image/video kind 显示临时占位文本;Task 6/7 实现后再替换为真实组件。这样 Task 5 能独立跑通文本流式。

- [ ] **Step 1: 写 RoutingBadge**

Create `control-panel/src/components/chat/RoutingBadge.tsx`:

```tsx
interface RoutingBadgeProps {
  intent?: 'understanding' | 'generation:image' | 'generation:video' | null
  model?: string
}

const MAP = {
  understanding: { icon: '🧠', label: '理解' },
  'generation:image': { icon: '🎨', label: '图片' },
  'generation:video': { icon: '🎬', label: '视频' },
} as const

export default function RoutingBadge({ intent, model }: RoutingBadgeProps) {
  if (!intent) return null
  const info = MAP[intent]
  if (!info) return null
  return (
    <div className="flex items-center gap-1 mb-1 text-xs" style={{ color: 'var(--color-text-secondary)' }}>
      <span>{info.icon}</span>
      <span>{info.label}</span>
      {model && <span style={{ opacity: 0.7 }}>· {model}</span>}
    </div>
  )
}
```

- [ ] **Step 2: 写 MessageBubble(含 classifyContent,image/video 占位)**

Create `control-panel/src/components/chat/MessageBubble.tsx`:

```tsx
import type { ChatPageMessage } from '@/types'
import RoutingBadge from './RoutingBadge'

export type ContentKind = 'image' | 'video' | 'text'

/** 双保险判定:intent 或 content 形态任一命中。 */
export function classifyContent(
  intent: ChatPageMessage['intent'],
  content: string,
): ContentKind {
  if (intent === 'generation:image') return 'image'
  if (intent === 'generation:video') return 'video'
  // content 启发式兜底
  if (/^https?:\/\//i.test(content) || /^data:image\//i.test(content)) return 'image'
  if (/id=[\w-]+/.test(content) && /poll\s+\/v1\/videos\//.test(content)) return 'video'
  return 'text'
}

interface MessageBubbleProps {
  msg: ChatPageMessage
  isStreaming: boolean
}

export default function MessageBubble({ msg, isStreaming }: MessageBubbleProps) {
  if (msg.role === 'user') {
    return (
      <div className="flex justify-end mb-4">
        <div className="max-w-[70%] px-4 py-2 rounded-lg" style={{ backgroundColor: 'var(--color-primary)', color: 'var(--color-text-inverse)' }}>
          <p className="whitespace-pre-wrap break-words">{msg.content}</p>
        </div>
      </div>
    )
  }

  const kind = classifyContent(msg.intent, msg.content)
  return (
    <div className="flex justify-start mb-4">
      <div
        className="max-w-[70%] px-4 py-2 rounded-lg"
        style={{
          backgroundColor: 'var(--color-bg-overlay)',
          color: 'var(--color-text-primary)',
          border: msg.error ? '1px solid var(--color-error, #e5484d)' : '1px solid var(--color-border)',
        }}
      >
        <RoutingBadge intent={msg.intent} model={msg.model} />
        {kind === 'image' && (
          <p style={{ color: 'var(--color-text-secondary)' }}>
            [图片占位]{!isStreaming ? '' : '…'}
          </p>
        )}
        {kind === 'video' && (
          <p style={{ color: 'var(--color-text-secondary)' }}>
            [视频占位]{!isStreaming ? '' : '…'}
          </p>
        )}
        {kind === 'text' && (
          <p className="whitespace-pre-wrap break-words">
            {msg.content}
            {isStreaming && <span className="animate-pulse">▌</span>}
          </p>
        )}
      </div>
    </div>
  )
}
```

- [ ] **Step 3: 验证编译**

Run: `cd control-panel && npx tsc -b --noEmit`
Expected: 退出码 0。

- [ ] **Step 4: Commit**

```bash
git add control-panel/src/components/chat/RoutingBadge.tsx control-panel/src/components/chat/MessageBubble.tsx
git commit -m "feat(chat): add MessageBubble with classifyContent and RoutingBadge (text first)"
```

---

## Task 6: ChatTimeline + ChatComposer + Chat 页面接通(文本可跑)

**Files:**
- Create: `control-panel/src/components/chat/ChatTimeline.tsx`
- Create: `control-panel/src/components/chat/ChatComposer.tsx`
- Modify: `control-panel/src/pages/Chat.tsx`(替换占位)

**Interfaces:**
- Consumes: `useChat`、`MessageBubble`。
- Produces: 可跑的文本聊天页(图片/视频仍占位)。

- [ ] **Step 1: 写 ChatTimeline**

Create `control-panel/src/components/chat/ChatTimeline.tsx`:

```tsx
import { useEffect, useRef } from 'react'
import type { ChatPageMessage } from '@/types'
import MessageBubble from './MessageBubble'

interface ChatTimelineProps {
  messages: ChatPageMessage[]
  streaming: boolean
  streamingId: string | null
}

export default function ChatTimeline({ messages, streaming, streamingId }: ChatTimelineProps) {
  const bottomRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  return (
    <div className="flex flex-col overflow-y-auto" style={{ height: '100%' }}>
      {messages.map(m => (
        <MessageBubble
          key={m.id}
          msg={m}
          isStreaming={streaming && m.id === streamingId}
        />
      ))}
      <div ref={bottomRef} />
    </div>
  )
}
```

- [ ] **Step 2: 写 ChatComposer**

Create `control-panel/src/components/chat/ChatComposer.tsx`:

```tsx
import { useState, useRef, type KeyboardEvent } from 'react'
import { Send, Square } from 'lucide-react'

interface ChatComposerProps {
  streaming: boolean
  disabled: boolean
  onSend: (text: string) => void
  onStop: () => void
}

export default function ChatComposer({ streaming, disabled, onSend, onStop }: ChatComposerProps) {
  const [text, setText] = useState('')
  const taRef = useRef<HTMLTextAreaElement>(null)

  function submit() {
    const t = text.trim()
    if (!t || streaming || disabled) return
    onSend(t)
    setText('')
    if (taRef.current) taRef.current.style.height = 'auto'
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      submit()
    }
  }

  function onInput() {
    const ta = taRef.current
    if (!ta) return
    ta.style.height = 'auto'
    ta.style.height = `${Math.min(ta.scrollHeight, 160)}px`
  }

  return (
    <div className="flex items-end gap-2 p-3" style={{ borderTop: '1px solid var(--color-border)' }}>
      <textarea
        ref={taRef}
        value={text}
        disabled={disabled}
        onChange={e => setText(e.target.value)}
        onInput={onInput}
        onKeyDown={onKeyDown}
        rows={1}
        placeholder={disabled ? '请先在任一页面设置 API Key' : '输入消息,Enter 发送 / Shift+Enter 换行'}
        className="flex-1 resize-none px-3 py-2 rounded-md outline-none"
        style={{
          backgroundColor: 'var(--color-bg-overlay)',
          color: 'var(--color-text-primary)',
          border: '1px solid var(--color-border)',
          maxHeight: '160px',
        }}
      />
      {streaming ? (
        <button
          onClick={onStop}
          className="flex items-center gap-1 px-3 py-2 rounded-md cursor-pointer"
          style={{ backgroundColor: 'var(--color-error, #e5484d)', color: '#fff' }}
        >
          <Square size={16} /> 停止
        </button>
      ) : (
        <button
          onClick={submit}
          disabled={disabled || !text.trim()}
          className="flex items-center gap-1 px-3 py-2 rounded-md cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
          style={{ backgroundColor: 'var(--color-primary)', color: 'var(--color-text-inverse)' }}
        >
          <Send size={16} /> 发送
        </button>
      )}
    </div>
  )
}
```

- [ ] **Step 3: 改 Chat.tsx 接通(含未设 key 空状态)**

Replace `control-panel/src/pages/Chat.tsx` 全文:

```tsx
import { getSavedApiKey } from '@/api/client'
import { useChat } from '@/hooks/useChat'
import ChatTimeline from '@/components/chat/ChatTimeline'
import ChatComposer from '@/components/chat/ChatComposer'
import { Trash2 } from 'lucide-react'

export default function Chat() {
  const { messages, streaming, error, send, stop, clear } = useChat()
  const hasKey = !!getSavedApiKey()

  if (!hasKey) {
    return (
      <div className="flex items-center justify-center" style={{ height: 'calc(100vh - 56px)' }}>
        <div className="text-center" style={{ color: 'var(--color-text-secondary)' }}>
          <p className="mb-2">请先在任一页面设置 API Key(右上角 / 其他页面输入框)。</p>
          <p className="text-sm" style={{ opacity: 0.7 }}>设置后回到本页即可开始聊天。</p>
        </div>
      </div>
    )
  }

  // 最后一条活跃助手消息 id(用于流式标记)
  const lastAssistant = [...messages].reverse().find(m => m.role === 'assistant')
  const streamingId = streaming ? (lastAssistant?.id ?? null) : null

  return (
    <div className="flex flex-col" style={{ height: 'calc(100vh - 56px)' }}>
      <div className="flex items-center justify-between px-1 py-2">
        <h2 className="text-md font-semibold" style={{ color: 'var(--color-text-primary)' }}>聊天</h2>
        <button
          onClick={clear}
          disabled={streaming || messages.length === 0}
          className="flex items-center gap-1 px-2 py-1 rounded-md text-sm cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
          style={{ color: 'var(--color-text-secondary)' }}
        >
          <Trash2 size={14} /> 清空
        </button>
      </div>
      {error && (
        <div className="mx-1 mb-2 px-3 py-2 rounded-md text-sm" style={{ backgroundColor: 'var(--color-error, #e5484d)', color: '#fff' }}>
          {error}
        </div>
      )}
      <div className="flex-1 min-h-0 mx-1 rounded-md" style={{ border: '1px solid var(--color-border)', backgroundColor: 'var(--color-bg-base)' }}>
        <ChatTimeline messages={messages} streaming={streaming} streamingId={streamingId} />
      </div>
      <div className="mx-1 mt-2 rounded-md" style={{ backgroundColor: 'var(--color-bg-elevated)' }}>
        <ChatComposer streaming={streaming} disabled={false} onSend={send} onStop={stop} />
      </div>
    </div>
  )
}
```

- [ ] **Step 4: 验证编译**

Run: `cd control-panel && npx tsc -b --noEmit`
Expected: 退出码 0。

- [ ] **Step 5: 手动验证文本聊天**

Run: `cd control-panel && npm run dev`(需后端 `:8000` 在跑)
设好 API key(若未设,在其他页设),打开 `/chat`,发"你好"→ 文本流式追加,带 🧠 badge + 模型名;发第二句能引用上下文;点停止能中断。Ctrl-C 停。

- [ ] **Step 6: Commit**

```bash
git add control-panel/src/components/chat/ChatTimeline.tsx control-panel/src/components/chat/ChatComposer.tsx control-panel/src/pages/Chat.tsx
git commit -m "feat(chat): wire ChatTimeline + ChatComposer + Chat page (text streaming works)"
```

---

## Task 7: MediaImage(done 后渲染)

**Files:**
- Create: `control-panel/src/components/chat/MediaImage.tsx`
- Modify: `control-panel/src/components/chat/MessageBubble.tsx`(替换 image 占位分支)

**Interfaces:**
- Consumes: 无外部依赖。
- Produces: `MediaImage` 组件,props `{ content: string; done: boolean }`。

- [ ] **Step 1: 写 MediaImage**

Create `control-panel/src/components/chat/MediaImage.tsx`:

```tsx
import { useState } from 'react'

interface MediaImageProps {
  content: string
  done: boolean
}

export default function MediaImage({ content, done }: MediaImageProps) {
  const [errored, setErrored] = useState(false)

  if (!done) {
    return (
      <div className="flex items-center gap-2 text-sm" style={{ color: 'var(--color-text-secondary)' }}>
        <span className="animate-pulse">🎨 生成图片中…</span>
      </div>
    )
  }

  if (errored) {
    return (
      <div className="text-sm" style={{ color: 'var(--color-error, #e5484d)' }}>
        图片加载失败
      </div>
    )
  }

  // content 可能是 URL 或 data: base64
  const src = content.startsWith('data:') || /^https?:\/\//i.test(content)
    ? content
    : `data:image/png;base64,${content}`

  return (
    <img
      src={src}
      alt="生成图片"
      onError={() => setErrored(true)}
      className="max-w-full rounded-md"
      style={{ maxHeight: '400px', border: '1px solid var(--color-border)' }}
    />
  )
}
```

- [ ] **Step 2: MessageBubble 替换 image 分支**

Modify `control-panel/src/components/chat/MessageBubble.tsx`:

顶部 import 加:

```tsx
import MediaImage from './MediaImage'
```

把 image 占位分支:

```tsx
        {kind === 'image' && (
          <p style={{ color: 'var(--color-text-secondary)' }}>
            [图片占位]{!isStreaming ? '' : '…'}
          </p>
        )}
```

替换为:

```tsx
        {kind === 'image' && (
          <MediaImage content={msg.content} done={!isStreaming} />
        )}
```

- [ ] **Step 3: 验证编译**

Run: `cd control-panel && npx tsc -b --noEmit`
Expected: 退出码 0。

- [ ] **Step 4: 手动验证图片**

`npm run dev`,在 `/chat` 发"画一只戴帽子的猫"→ 🎨 badge,流式中显示"生成图片中…",done 后渲染 `<img>`(中途不闪半截 `<img>`)。Ctrl-C 停。

- [ ] **Step 5: Commit**

```bash
git add control-panel/src/components/chat/MediaImage.tsx control-panel/src/components/chat/MessageBubble.tsx
git commit -m "feat(chat): render images after stream done"
```

---

## Task 8: MediaVideo(轮询 + 渲染)

**Files:**
- Create: `control-panel/src/components/chat/MediaVideo.tsx`
- Modify: `control-panel/src/components/chat/MessageBubble.tsx`(替换 video 占位分支,加 done prop)

**Interfaces:**
- Consumes: `getVideoStatus`(`api/client.ts`)、`VideoStatusResponse`(types.ts)。
- Produces: `MediaVideo` 组件,props `{ content: string; done: boolean }`。

- [ ] **Step 1: 写 MediaVideo**

Create `control-panel/src/components/chat/MediaVideo.tsx`:

```tsx
import { useEffect, useRef, useState } from 'react'
import { getVideoStatus } from '@/api/client'

type Phase = 'idle' | 'polling' | 'succeeded' | 'failed' | 'timeout'

interface MediaVideoProps {
  content: string
  done: boolean
}

const POLL_INTERVAL_MS = 3000
const TIMEOUT_MS = 120000

function parseVideoId(content: string): string | null {
  const m = content.match(/id=([\w-]+)/)
  return m ? m[1] : null
}

export default function MediaVideo({ content, done }: MediaVideoProps) {
  const [phase, setPhase] = useState<Phase>('idle')
  const [videoUrl, setVideoUrl] = useState<string | null>(null)
  const [elapsed, setElapsed] = useState(0)
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const videoId = done ? parseVideoId(content) : null

  useEffect(() => {
    if (!done || !videoId) return
    setPhase('polling')
    setElapsed(0)
    let cancelled = false
    const start = Date.now()

    async function poll() {
      if (cancelled) return
      const e = Date.now() - start
      setElapsed(e)
      if (e > TIMEOUT_MS) {
        setPhase('timeout')
        return
      }
      try {
        const st = await getVideoStatus(videoId!)
        if (cancelled) return
        if (st.status === 'succeeded' && st.video?.url) {
          setVideoUrl(st.video.url)
          setPhase('succeeded')
          return
        }
        if (st.status === 'failed' || st.error) {
          setPhase('failed')
          return
        }
        // queued / in_progress → 继续轮询
      } catch {
        if (cancelled) return
        // 单次查询失败,不立即终止,下一轮重试(直到超时)
      }
    }

    poll()
    timerRef.current = setInterval(poll, POLL_INTERVAL_MS)
    return () => {
      cancelled = true
      if (timerRef.current) clearInterval(timerRef.current)
    }
  }, [done, videoId])

  if (!done) {
    return (
      <div className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
        <span className="animate-pulse">🎬 提交视频任务中…</span>
      </div>
    )
  }

  if (!videoId) {
    return <div className="text-sm" style={{ color: 'var(--color-error, #e5484d)' }}>无法解析视频任务 id</div>
  }

  if (phase === 'succeeded' && videoUrl) {
    return (
      <video
        src={videoUrl}
        controls
        className="max-w-full rounded-md"
        style={{ maxHeight: '400px', border: '1px solid var(--color-border)' }}
      />
    )
  }

  if (phase === 'failed') {
    return (
      <div className="text-sm" style={{ color: 'var(--color-error, #e5484d)' }}>
        视频生成失败
      </div>
    )
  }

  if (phase === 'timeout') {
    return (
      <div className="text-sm" style={{ color: 'var(--color-error, #e5484d)' }}>
        视频生成超时({Math.round(TIMEOUT_MS / 1000)}s)
      </div>
    )
  }

  // polling
  return (
    <div className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
      <span className="animate-pulse">🎬 生成视频中…</span>
      <span className="ml-2" style={{ opacity: 0.7 }}>{Math.round(elapsed / 1000)}s</span>
    </div>
  )
}
```

- [ ] **Step 2: MessageBubble 替换 video 分支**

Modify `control-panel/src/components/chat/MessageBubble.tsx`:

顶部 import 加:

```tsx
import MediaVideo from './MediaVideo'
```

把 video 占位分支:

```tsx
        {kind === 'video' && (
          <p style={{ color: 'var(--color-text-secondary)' }}>
            [视频占位]{!isStreaming ? '' : '…'}
          </p>
        )}
```

替换为:

```tsx
        {kind === 'video' && (
          <MediaVideo content={msg.content} done={!isStreaming} />
        )}
```

- [ ] **Step 3: 验证编译**

Run: `cd control-panel && npx tsc -b --noEmit`
Expected: 退出码 0。

- [ ] **Step 4: 手动验证视频**

`npm run dev`,`/chat` 发"生成一段视频"→ 🎬 badge,显示"生成视频中…Ns",完成后渲染 `<video controls>`;切走页面再回来,轮询重启;刷新页面,历史视频消息重新轮询。Ctrl-C 停。

- [ ] **Step 5: Commit**

```bash
git add control-panel/src/components/chat/MediaVideo.tsx control-panel/src/components/chat/MessageBubble.tsx
git commit -m "feat(chat): poll and render video output"
```

---

## Task 9: smoke 脚本 + 手动清单验证 + 文档更新

**Files:**
- Create: `scripts/smoke_chat.sh`
- Modify: `CLAUDE.md`(更新 "Dead frontend code" 条目)

**Interfaces:** 无。

- [ ] **Step 1: 写 smoke 脚本**

Create `scripts/smoke_chat.sh`:

```bash
#!/usr/bin/env bash
# 控制台聊天窗 MVP —— 后端 SSE 形态烟测。
# 用法: ADMIN_KEY=xxx GATEWAY=http://localhost:8000 bash scripts/smoke_chat.sh
set -euo pipefail

ADMIN_KEY="${ADMIN_KEY:?需要 ADMIN_KEY 环境变量}"
GATEWAY="${GATEWAY:-http://localhost:8000}"

echo "==> 文本意图 SSE 烟测"
text_resp=$(curl -sN -X POST "$GATEWAY/v1/chat/completions" \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","stream":true,"messages":[{"role":"user","content":"你好"}]}' || true)
echo "$text_resp" | grep -q '"delta"' || { echo "FAIL: 文本 SSE 未出现 delta"; exit 1; }
echo "$text_resp" | grep -q '\[DONE\]' || { echo "FAIL: 未收到 [DONE]"; exit 1; }
echo "PASS: 文本 SSE 形态正常"

echo "==> 图片意图 SSE 烟测"
img_resp=$(curl -sN -X POST "$GATEWAY/v1/chat/completions" \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","stream":true,"messages":[{"role":"user","content":"画一只猫"}]}' || true)
echo "$img_resp" | grep -q '"generation:image"' || { echo "WARN: 未检测到 generation:image intent(可能后端分类未命中,人工确认)"; }
echo "$img_resp" | grep -qE '"content":"https?://|"content":"data:image/' \
  && echo "PASS: 图片 content 是 URL/b64" \
  || echo "WARN: 图片 content 非预期 URL/b64 形态(人工确认)"

echo "==> 全部烟测完成"
```

赋权:

```bash
chmod +x scripts/smoke_chat.sh
```

- [ ] **Step 2: 跑 smoke 脚本(若后端在跑)**

Run: `ADMIN_KEY=<your-key> bash scripts/smoke_chat.sh`
Expected: 文本 PASS;图片 PASS 或 WARN(intent 分类靠后端,可能因模型未配图能力而 WARN——人工确认即可)。

- [ ] **Step 3: 跑手动清单**

`npm run dev`,逐条验证 spec §9.1 清单:
- [ ] 未设 key 打开 `/chat` → 空状态提示
- [ ] 发"你好"→ 文本流式,🧠 badge + 模型名
- [ ] 多轮引用上下文
- [ ] 发"画一只猫"→ 🎨 badge,done 后 `<img>`(不闪半截)
- [ ] 发"生成一段视频"→ 🎬 badge,轮询后 `<video controls>`
- [ ] 视频超时 → 超时文案
- [ ] 流式中停止 → 保留文本
- [ ] 断网 → 错误提示
- [ ] 刷新 → 历史重现,视频重新轮询
- [ ] 清空 → 历史清空 + localStorage 删
- [ ] 侧栏"聊天"入口 + `/chat` 可达

- [ ] **Step 4: 更新 CLAUDE.md**

在 `CLAUDE.md` 的 "Known States & Gotchas" → "Dead frontend code" 条目里,把 `createChatCompletionStream` 从 reserved 列表移除(标注已在 chat 页使用)。定位该条:

```
- **Dead frontend code** — `hooks/useAuth.ts`, `hooks/usePoll.ts` have 0 imports. Six API client fns (`createChatCompletion*`, `listModels`, `createEmbeddings`, `getQuota`, `getMetricsJson`) are reserved for Entry B.
```

改为:

```
- **Dead frontend code** — `hooks/useAuth.ts`, `hooks/usePoll.ts` have 0 imports. Five API client fns (`createChatCompletion` non-stream, `listModels`, `createEmbeddings`, `getQuota`, `getMetricsJson`) are reserved for Entry B. `createChatCompletionStream` + new `getVideoStatus` now used by the `/chat` page (聊天窗 MVP).
```

并在 `control-panel/src/` 包布局说明里加一句 `/chat` 页面与 `components/chat/` 目录存在(若 CLAUDE.md 有 control-panel 布局段落)。

- [ ] **Step 5: 最终构建验证**

Run: `cd control-panel && npm run build`
Expected: `tsc -b && vite build` 成功退出码 0。

- [ ] **Step 6: Commit**

```bash
git add scripts/smoke_chat.sh CLAUDE.md
git commit -m "test(chat): add smoke script and update CLAUDE.md for chat window MVP"
```

---

## Self-Review

**1. Spec coverage:**
- §2 目标 1(侧栏+路由)→ Task 1 ✓
- §2 目标 2(文本多轮)→ Task 4(useChat 发全量历史)+ Task 6 ✓
- §2 目标 3(图片渲染)→ Task 7 ✓
- §2 目标 4(视频轮询渲染)→ Task 8 ✓
- §2 目标 5(路由 badge + 模型名)→ Task 5 ✓
- §2 目标 6(localStorage 持久化)→ Task 4 ✓
- §2 目标 7(复用认证)→ Task 6(空状态)✓
- §3 响应契约 → Task 4(SSE 解析)+ Task 5(classifyContent 双保险)✓
- §7 localStorage → Task 4(debounce 500ms, key 名一致)✓
- §8 错误处理 → Task 4(abort/4xx/占位移除)+ Task 6(error 提示)+ Task 8(轮询失败/超时)✓
- §9 测试 → Task 9(smoke + 手动清单)✓
- 无遗漏。

**2. Placeholder scan:** 无 "TBD/TODO/实现细节后填"。所有代码步骤含完整代码。

**3. Type consistency:**
- `ChatPageMessage` 定义(Task 2)与使用(Task 4/5/6)字段一致:`id/role/content/intent/model/error/ts`。✓
- `classifyContent` 返回 `ContentKind`(Task 5)在 `MessageBubble` 用 `kind` 变量一致。✓
- `useChat` 返回 `{ messages, streaming, error, send, stop, clear }`(Task 4)与 `Chat.tsx`(Task 6)解构一致。✓
- `MediaImage`/`MediaVideo` props `{content, done}`(Task 7/8)与 `MessageBubble` 调用 `done={!isStreaming}` 一致。✓
- `getVideoStatus(videoId): Promise<VideoStatusResponse>`(Task 3)与 `MediaVideo`(Task 8)调用一致。✓
- `VideoStatusResponse` 字段 `{id?, status?, video?: {url?}, error?}`(Task 2)与 `MediaVideo` 读 `st.status`/`st.video?.url`/`st.error` 一致。✓
