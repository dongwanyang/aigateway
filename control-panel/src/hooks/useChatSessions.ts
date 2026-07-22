import { useCallback, useEffect, useRef, useState } from 'react'
import {
  requestChatCompletion,
  getDraftPreview,
  getDraftResult,
  confirmDraft,
  rejectDraft,
  deleteSessionDrafts,
  getVideoStatus,
} from '@/api/client'
import type { ChatPageMessage, ChatMessage, ChatSession, ChatDraftState, VideoStatusResponse } from '@/types'

const SESSIONS_KEY = 'aigateway:chat:sessions'
const ACTIVE_KEY = 'aigateway:chat:active'
const LEGACY_MESSAGES_KEY = 'aigateway:chat:messages'

let idCounter = 0
function nextId(): string {
  idCounter += 1
  return `msg-${Date.now()}-${idCounter}`
}

/** 已处理过刷新续传的会话 ID 集合(模块级)。 */
const resumedSessionIds = new Set<string>()

/** 正在轮询的视频任务 ID 集合，防止重复轮询。 */
const pollingVideoIds = new Set<string>()

/** 视频轮询间隔（毫秒） */
const VIDEO_POLL_INTERVAL_MS = 5000

/** 视频轮询最大次数，超时后停止（约 30 分钟） */
const VIDEO_POLL_MAX_ATTEMPTS = 360

/** 清理所有正在进行的视频轮询 */
function clearAllPolling() {
  pollingVideoIds.clear()
}

function newSessionId(): string {
  return `sess-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
}

function titleFromMessages(messages: ChatPageMessage[]): string {
  const firstUser = messages.find(m => m.role === 'user')
  if (!firstUser) return '新对话'
  const t = firstUser.content.trim().slice(0, 20)
  return t || '新对话'
}

/** 判断消息是否包含活跃的异步任务（视频/草稿）。 */
function hasActiveAsyncTask(msg: ChatPageMessage): boolean {
  // 视频任务：有 videoId 且未标记 error/incomplete
  if (msg.videoId && !msg.error && !msg.incomplete) return true
  // 草稿任务：pending/confirming/rejecting 状态
  if (msg.draft && ['pending', 'confirming', 'rejecting'].includes(msg.draft.status)) return true
  return false
}

/** 判断视频任务是否已完成（成功或失败）。 */
function isVideoTerminal(status: string | undefined): boolean {
  return status === 'succeeded' || status === 'failed' || status === 'error' || status === 'expired'
}

/** 从 localStorage 加载 sessions,无则迁移旧单会话 key,再无则空数组。 */
function loadSessions(): ChatSession[] {
  try {
    const raw = localStorage.getItem(SESSIONS_KEY)
    if (raw) {
      const parsed = JSON.parse(raw)
      if (Array.isArray(parsed)) return parsed as ChatSession[]
    }
  } catch {
    // 损坏,继续走迁移
  }
  // 迁移旧 aigateway:chat:messages(单会话)→ 单个 session
  try {
    const legacyRaw = localStorage.getItem(LEGACY_MESSAGES_KEY)
    if (legacyRaw) {
      const legacy = JSON.parse(legacyRaw)
      if (Array.isArray(legacy) && legacy.length > 0) {
        const now = Date.now()
        const migrated: ChatSession = {
          id: 'migrated',
          title: titleFromMessages(legacy as ChatPageMessage[]),
          messages: legacy as ChatPageMessage[],
          createdAt: now,
          updatedAt: now,
        }
        // 落盘 + 清旧 key
        try {
          localStorage.setItem(SESSIONS_KEY, JSON.stringify([migrated]))
          localStorage.setItem(ACTIVE_KEY, migrated.id)
          localStorage.removeItem(LEGACY_MESSAGES_KEY)
        } catch {
          // quota,忽略
        }
        return [migrated]
      }
    }
  } catch {
    // 旧数据损坏,丢弃
  }
  return []
}

function loadActiveId(sessions: ChatSession[]): string | null {
  try {
    const id = localStorage.getItem(ACTIVE_KEY)
    if (id && sessions.some(s => s.id === id)) return id
  } catch {
    // ignore
  }
  return sessions[0]?.id ?? null
}

/** 序列化 sessions 时剥离 draft 的 data URL(体积大,localStorage 装不下)。 */
function serializeSessions(sessions: ChatSession[]): string {
  const stripped = sessions.map(s => ({
    ...s,
    messages: s.messages.map(m => {
      if (!m.draft) return m
      const { previewDataUrl: _p, resultDataUrl: _r, ...draftRest } = m.draft
      return { ...m, draft: draftRest as ChatDraftState }
    }),
  }))
  return JSON.stringify(stripped)
}

export interface UseChatSessions {
  sessions: ChatSession[]
  activeId: string | null
  active: ChatSession | null
  streaming: boolean
  error: string | null
  pendingAssistantId: string | null
  newSession: () => void
  selectSession: (id: string) => void
  deleteSession: (id: string) => void
  send: (text: string) => Promise<void>
  stop: () => void
  clearActive: () => void
  confirmDraftMsg: (msgId: string) => Promise<void>
  rejectDraftMsg: (msgId: string) => Promise<void>
}

export function useChatSessions(): UseChatSessions {
  const [sessions, setSessions] = useState<ChatSession[]>(loadSessions)
  const [activeId, setActiveId] = useState<string | null>(() => loadActiveId(loadSessions()))
  const [streaming, setStreaming] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // 当前正在等待回复的助手消息 ID(空 content 占位)。用于在切换会话后仍能在
  // 原会话上显示三点动画——streaming=false 不代表该消息不需要提示。
  const [pendingAssistantId, setPendingAssistantId] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const inflightRef = useRef(false)
  // 用于触发轮询恢复：每次 activeId 变化时递增，确保刷新/切换会话后轮询 effect 重新运行
  const [resumePollingKey, setResumePollingKey] = useState(0)
  // 若当前 in-flight send 是刷新续传,记录其 session id。StrictMode 卸载时 abort 会中断它,
  // 此时需把该 id 从 resumedSessionIds 移除,让重挂载后的 effect 能重新续传(否则 Set 永久阻塞 → dev 下续传永不触发)。
  const resumeSessionRef = useRef<string | null>(null)
  // 最新 sessions 的镜像,供 send 闭包同步读取(避免 setSessions 回调里偷传 wire)
  const sessionsRef = useRef<ChatSession[]>(sessions)
  useEffect(() => { sessionsRef.current = sessions }, [sessions])

  // 确保至少有一个会话
  useEffect(() => {
    if (sessions.length === 0) {
      const now = Date.now()
      const s: ChatSession = { id: newSessionId(), title: '新对话', messages: [], createdAt: now, updatedAt: now }
      setSessions([s])
      setActiveId(s.id)
    } else if (!activeId || !sessions.some(s => s.id === activeId)) {
      setActiveId(sessions[0].id)
    }
  }, [sessions, activeId])

  // 组件卸载时 abort 上游。StrictMode dev 下会模拟一次卸载:若中断的是续传 send,
  // 把该 session id 从 resumedSessionIds 移除,使重挂载能重新续传(否则 Set 永久阻塞 → dev 续传失效)。
  useEffect(() => {
    return () => {
      const rs = resumeSessionRef.current
      if (rs) {
        resumedSessionIds.delete(rs)
        resumeSessionRef.current = null
      }
      abortRef.current?.abort()
      abortRef.current = null
      // StrictMode 模拟卸载会中断 mount#1 的续传 send,但 mount#1 的 finally(清 inflightRef)是 microtask,
      // 还没跑。mount#2 的 send 会因 inflightRef=true 直接 return → 续传彻底丢失。
      // 卸载时同步清掉,让 mount#2 的 send 能进入。
      inflightRef.current = false
    }
  }, [])

  // 活跃异步任务轮询：activeId 变化或组件重新挂载时重置轮询触发器，确保刷新/切换会话后自动恢复轮询
  useEffect(() => {
    setResumePollingKey(prev => prev + 1)
  }, [activeId])

  // debounce 持久化 — token 流式更新走这个，500ms 防抖足够（不需要每次 token 都写磁盘）
  useEffect(() => {
    const t = setTimeout(() => {
      try {
        localStorage.setItem(SESSIONS_KEY, serializeSessions(sessions))
      } catch {
        // quota / 序列化失败,静默
      }
    }, 500)
    return () => clearTimeout(t)
  }, [sessions])

  /** 关键状态转换立即落盘 — 不经过 debounce。
   *
   * 以下场景必须调用，否则 500ms 窗口期内刷新会丢失状态：
   * - Draft 响应到达（assistant 从空占位 → 有 draft）
   * - SSE 流正常完成（assistant 从空占位/半截内容 → 完整文本）
   * - 流中断标记 incomplete
   * - 错误发生
   *
   * Token 流式增量更新不需要立即落盘：resume effect 只看
   * incomplete/draft/videoId/error/content 这几个字段，
   * 中间 token 丢了也不影响续传逻辑。
   */
  const flushToStorage = useCallback(() => {
    try {
      localStorage.setItem(SESSIONS_KEY, serializeSessions(sessionsRef.current))
    } catch {
      // quota / 序列化失败,静默
    }
  }, [])

  // 硬刷新/关闭页面时,500ms debounce 可能还没落盘(尤其流式中断刚标 incomplete 就刷新)。
  // pagehide 同步 flush,确保 incomplete 标记写入 localStorage,否则重载后续传判断会漏掉。
  // 另:硬刷新会直接卸载页面,abort catch(设 incomplete)来不及跑。所以 flush 时若仍在流式输出,
  // 主动把末尾 assistant 标 incomplete,使重载后能触发续传。
  const streamingRef = useRef(false)
  useEffect(() => { streamingRef.current = streaming }, [streaming])
  useEffect(() => {
    const flush = () => {
      try {
        let toFlush = sessionsRef.current
        if (streamingRef.current) {
          toFlush = toFlush.map(s => {
            const last = s.messages[s.messages.length - 1]
            if (last?.role === 'assistant' && last.content && !last.incomplete && !last.draft) {
              const msgs = s.messages.slice(0, -1).concat({ ...last, incomplete: true })
              return { ...s, messages: msgs }
            }
            return s
          })
        }
        localStorage.setItem(SESSIONS_KEY, serializeSessions(toFlush))
      } catch {
        // ignore
      }
    }
    window.addEventListener('pagehide', flush)
    return () => window.removeEventListener('pagehide', flush)
  }, [])

  useEffect(() => {
    if (activeId) {
      try { localStorage.setItem(ACTIVE_KEY, activeId) } catch { /* ignore */ }
    }
  }, [activeId])

  const patchActiveMessages = useCallback(
    (updater: (msgs: ChatPageMessage[]) => ChatPageMessage[]) => {
      setSessions(prev => {
        const next = prev.map(s => {
          if (s.id !== activeId) return s
          const messages = updater(s.messages)
          const title = s.title === '新对话' && messages.some(m => m.role === 'user')
            ? titleFromMessages(messages)
            : s.title
          return { ...s, messages, title, updatedAt: Date.now() }
        })
        // 同步更新 ref,使同一事件循环内的 flushToStorage/pagehide 能读到最新状态。
        // 否则 React 的 useEffect 写 ref 发生在渲染后,500ms debounce 或立即 flush
        // 可能读到旧状态,导致 draft/incomplete 等关键标记丢失(刷新后误续传)。
        sessionsRef.current = next
        return next
      })
    },
    [activeId],
  )

  const patchMessage = useCallback(
    (msgId: string, updater: (m: ChatPageMessage) => ChatPageMessage) => {
      patchActiveMessages(msgs => msgs.map(m => (m.id === msgId ? updater(m) : m)))
    },
    [patchActiveMessages],
  )

  const stop = useCallback(() => {
    abortRef.current?.abort()
    abortRef.current = null
    // stop() 把 abortRef 清空后,被中断 send 的 finally 所有权检查(abortRef===controller)会失败,
    // 不再重置 inflightRef → 之后所有 send 都被 inflightRef 挡住,聊天假死。这里同步清掉。
    inflightRef.current = false
    resumeSessionRef.current = null
    setStreaming(false)
    // 注意:不清 pendingAssistantId！用户切换会话再切回来时,三点仍需显示。
    // pendingAssistantId 由 send() 完成/失败时清除。
  }, [])

  const newSession = useCallback(() => {
    if (streaming) stop()
    const now = Date.now()
    const s: ChatSession = { id: newSessionId(), title: '新对话', messages: [], createdAt: now, updatedAt: now }
    setSessions(prev => [s, ...prev])
    setActiveId(s.id)
    setError(null)
  }, [streaming, stop])

  const selectSession = useCallback((id: string) => {
    if (streaming) stop()
    setActiveId(id)
    setError(null)
  }, [streaming, stop])

  const deleteSession = useCallback(async (id: string) => {
    // 删的是正在流式输出的 active 会话 → 必须中止上游,否则 send 闭包仍持有旧 activeId,
    // fetch 会继续跑到结束(空转烧 token/配额,patch 因会话已删而成 no-op)。
    if (id === activeId) stop()
    // 先清后端草稿文件(异步,不阻塞 UI)
    void deleteSessionDrafts(id).catch((e) => {
      console.warn('删除会话草稿失败:', e instanceof Error ? e.message : e)
    })
    setSessions(prev => {
      const next = prev.filter(s => s.id !== id)
      // 若删的是 active,切到第一个
      if (id === activeId) {
        if (next.length > 0) {
          setActiveId(next[0].id)
        } else {
          const now = Date.now()
          const fresh: ChatSession = { id: newSessionId(), title: '新对话', messages: [], createdAt: now, updatedAt: now }
          setActiveId(fresh.id)
          return [fresh]
        }
      }
      return next
    })
  }, [activeId, stop])

  const clearActive = useCallback(() => {
    stop()
    // 清空后会话状态已变,旧的续传标记失效:移出 Set,使后续新发+刷新能正常续传。
    if (activeId) resumedSessionIds.delete(activeId)
    patchActiveMessages(() => [])
  }, [stop, patchActiveMessages, activeId])

  /** 核心:发送一条用户消息。resume=true 时不重复追加 user 消息(续传场景)。
   *  dropLastAssistant=true:wire 历史去掉末尾那条 assistant(用于 incomplete 续传——
   *  末尾 assistant 内容是上次中断的半截,不能当完整轮次发回后端,否则污染模型上下文)。 */
  const send = useCallback(async (text: string, opts?: { resume?: boolean; dropLastAssistant?: boolean }) => {
    const trimmed = text.trim()
    if (!trimmed || streaming || inflightRef.current) return
    inflightRef.current = true
    const isResume = !!opts?.resume
    setError(null)
    // 用户新发一条(非续传)→ 会话状态已变,旧的续传标记失效:移出 Set,
    // 使本次发送若被刷新中断,重载后能正常续传(否则 Set 永久阻塞)。
    if (!isResume && activeId) resumedSessionIds.delete(activeId)

    const userMsg: ChatPageMessage = {
      id: nextId(), role: 'user', content: trimmed, ts: Date.now(),
    }
    const assistantId = nextId()
    const assistantMsg: ChatPageMessage = {
      id: assistantId, role: 'assistant', content: '', ts: Date.now(),
    }

    // 续传:user 消息已在历史里,不再追加;否则追加 user + 空 assistant 占位
    if (opts?.resume) {
      patchActiveMessages(msgs => [...msgs, assistantMsg])
    } else {
      patchActiveMessages(msgs => [...msgs, userMsg, assistantMsg])
    }
    setPendingAssistantId(assistantId)

    // wire 历史 = 当前会话消息(续传时不重复追加本次 user,因其已在历史里)
    const cur = sessionsRef.current.find(x => x.id === activeId)
    let baseMsgs = cur?.messages ?? []

    // 续传时只发送最近的消息,避免重发整个历史
    if (isResume && baseMsgs.length > 10) {
      baseMsgs = baseMsgs.slice(-10) // 只保留最后10条消息
    }

    // incomplete 续传:去掉末尾 assistant。resume 时上面刚追加了一个空占位,
    // patchActiveMessages 已同步更新 sessionsRef,因此 baseMsgs 末尾就是这个空占位;
    // 如果是旧代码路径,末尾也可能是未持久化的 incomplete assistant。无论哪种,
    // 显式切掉避免把不完整的 assistant 发回后端。
    if (opts?.dropLastAssistant && baseMsgs.length > 0 && baseMsgs[baseMsgs.length - 1].role === 'assistant') {
      baseMsgs = baseMsgs.slice(0, -1)
    }
    const wireMessages: ChatMessage[] = (opts?.resume ? [...baseMsgs] : [...baseMsgs, userMsg])
      .filter(m => m.role === 'user' || (m.role === 'assistant' && m.content && !m.draft))
      .map(m => ({ role: m.role, content: m.content }))

    setStreaming(true)
    const controller = new AbortController()
    abortRef.current = controller
    // 续传 send:记录 session id,供 StrictMode 卸载时判断是否需从 resumedSessionIds 移除。
    if (isResume) resumeSessionRef.current = activeId
    let reader: ReadableStreamDefaultReader<Uint8Array> | null = null

    try {
      const resp = await requestChatCompletion(
        { model: 'auto', messages: wireMessages, stream: true, chat_session_id: activeId ?? undefined },
        controller.signal,
      )

      if (resp.kind === 'draft') {
        // 草稿分支:不读流,把 assistant 占位转为草稿消息,拉预览图
        const draft: ChatDraftState = {
          draftId: resp.draftId,
          previewUrl: resp.previewUrl,
          mediaType: resp.mediaType,
          status: 'pending',
        }
        patchMessage(assistantId, m => ({
          ...m,
          intent: resp.mediaType === 'image' ? 'generation:image' : 'generation:video',
          model: 'draft',
          draft,
        }))
        // 关键状态转换: draft 响应到达，立即落盘防止 debounce 窗口期内刷新导致重发
        flushToStorage()
        setStreaming(false)
        abortRef.current = null
        inflightRef.current = false  // 主请求已完成,预览拉取是 best-effort,不应阻塞下一条 send
        // 异步拉预览图(不阻塞 streaming 状态)
        try {
          const { previewDataUrl } = await getDraftPreview(resp.draftId)
          patchMessage(assistantId, m => m.draft
            ? { ...m, draft: { ...m.draft, previewDataUrl } }
            : m)
        } catch (e) {
          const code = e instanceof Error ? e.message : '预览加载失败'
          patchMessage(assistantId, m => m.draft
            ? { ...m, draft: { ...m.draft, status: code.includes('not_found') || code.includes('expired') ? 'expired' : 'error', errorMessage: code } }
            : m)
        }
        return
      }

      // 流式分支:按 SSE 帧累加
      reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
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
            inflightRef.current = false
            // 注意:不在此处 reader.releaseLock() —— finally 会统一释放,
            // 重复 releaseLock 会抛 TypeError(ReadableStream spec)→ unhandled rejection。
            return
          }
          try {
            const chunk = JSON.parse(payload)
            const delta = chunk?.choices?.[0]?.delta
            const meta = chunk?._meta?.routed_to
            const isErr = !!chunk?.error
            patchMessage(assistantId, m => {
              const next: ChatPageMessage = { ...m }
              if (delta?.content) {
                next.content += delta.content
                setPendingAssistantId(null)
              }
              if (meta?.intent && !next.intent) next.intent = meta.intent
              if (meta?.model && !next.model) next.model = meta.model
              // 提取视频生成任务的 ID，用于刷新后轮询恢复
              const videoId = chunk?._meta?.video_id
              if (videoId && !next.videoId) next.videoId = videoId
              if (isErr) next.error = true
              return next
            })
          } catch {
            // 非 JSON 帧,跳过
          }
        }
      }
      setStreaming(false)
    } catch (e) {
      if (controller.signal.aborted) {
        // 标记 incomplete(刷新续传依据)。reader 释放交给 finally,避免重复 releaseLock 抛 TypeError。
        patchMessage(assistantId, m => (m.content ? { ...m, incomplete: true } : m))
        setStreaming(false)
      } else {
        const msg = e instanceof Error ? e.message : '请求失败'
        setError(msg)
        // 移除空占位
        setSessions(prev => prev.map(s => s.id === activeId
          ? { ...s, messages: s.messages.filter(m => !(m.id === assistantId && m.content === '' && !m.draft)) }
          : s))
        setStreaming(false)
      }
    } finally {
      // 释放 reader 锁。流已关闭/出错时 releaseLock 可能抛 TypeError,吞掉即可。
      try { reader?.releaseLock() } catch { /* reader 已释放或流已关闭 */ }
      // 所有权检查:仅当当前 send 仍持有 controller 时才清 ref。
      // draft 分支会提前清 ref 并 return,期间用户可能已发起 Send B(设了新 controller/inflightRef=true),
      // 无条件覆写会把 B 的 ref 冲掉 → stop() 失效 + 并发流污染。
      if (abortRef.current === controller) {
        abortRef.current = null
        inflightRef.current = false
        // 续传 send 已完成,清掉 resume 标记(仅当仍归本 send 所有)。
        if (isResume) resumeSessionRef.current = null
      }
    }
  }, [streaming, activeId, patchActiveMessages, patchMessage])

  // sendRef:resume effect 通过它调用 send,而不把 send 放进 effect 依赖数组。
  // 否则 send 依赖 streaming,setStreaming(true) 时 send 引用变化 → effect 重跑 →
  // 在 send 刚追加的空 assistant 占位上误判为"中断占位"并 slice 掉 → 草稿响应回来
  // patchMessage(assistantId) 找不到消息 → 草稿丢失(ISSUE-002)。
  const sendRef = useRef(send)
  useEffect(() => { sendRef.current = send }, [send])

  // 刷新续传:mount 或切换会话时检测 active 会话末尾,未完成则重发;并补拉所有草稿的预览图。
  // 用模块级 resumedSessionIds 防御 StrictMode 双 mount(见该 Set 注释)。
  //
  // 关键:依赖只列 [activeId],不列 sessions。否则用户正常 send 一条消息时 sessions 变化 →
  // effect 重跑(因 send 第 333 行 delete 了 resumedSessionIds)→ 看到 send 刚追加的空 assistant
  // 占位(last.role==='assistant' 且 !content && !draft)→ slice 掉它 + 试图续传。占位被删后,
  // 原 send 的草稿响应回来 patchMessage(assistantId) 找不到消息 → 草稿永不渲染。
  // 通过 sessionsRef 读取最新快照,既拿到当前消息又不在 sessions 变化时重触发。
  useEffect(() => {
    if (!activeId || resumedSessionIds.has(activeId)) return
    const s = sessionsRef.current.find(x => x.id === activeId)
    if (!s || s.messages.length === 0) return
    // 有内容需处理才标记;空会话不标(否则 clearActive 后同会话再发+刷新会被永久阻塞续传)。
    resumedSessionIds.add(activeId)

    // 2) 先补拉所有草稿消息的预览图(data URL 不持久化,刷新后全丢)
    //    pending/confirming/rejecting → 降级 pending;confirmed → 保留状态;
    //    error/expired → 不动。若草稿已被后端回收 → 标记 expired。
    for (const m of s.messages) {
      if (m.role !== 'assistant' || !m.draft) continue
      const st = m.draft.status
      if (st === 'pending' || st === 'confirming' || st === 'rejecting') {
        patchMessage(m.id, mm => mm.draft
          ? { ...mm, draft: { ...mm.draft, status: 'pending', previewDataUrl: undefined, errorMessage: undefined } }
          : mm)
      } else if (st === 'confirmed') {
        // 高清图:后端文件存储已持久化,刷新后重取(不标记 resultLost)。
        patchMessage(m.id, mm => mm.draft
          ? { ...mm, draft: { ...mm.draft, previewDataUrl: undefined, resultDataUrl: undefined } }
          : mm)
        void getDraftResult(m.draft.draftId).then(
          ({ resultDataUrl }) => patchMessage(m.id, mm => mm.draft
            ? { ...mm, draft: { ...mm.draft, resultDataUrl } }
            : mm),
          (e: unknown) => {
            const code = e instanceof Error ? e.message : '结果加载失败'
            patchMessage(m.id, mm => mm.draft
              ? { ...mm, draft: { ...mm.draft, resultLost: true, errorMessage: code } }
              : mm)
          },
        )
      } else {
        continue // error/expired 不补拉
      }
      void getDraftPreview(m.draft.draftId).then(
        ({ previewDataUrl }) => patchMessage(m.id, mm => mm.draft
          ? { ...mm, draft: { ...mm.draft, previewDataUrl } }
          : mm),
        (e: unknown) => {
          const code = e instanceof Error ? e.message : '预览加载失败'
          patchMessage(m.id, mm => mm.draft
            ? { ...mm, draft: { ...mm.draft, status: 'expired', errorMessage: code } }
            : mm)
        },
      )
    }

    // 检查是否有活跃的异步任务（视频/草稿），如果有则跳过续传
    const hasActiveAsyncTaskInLastMsg = s.messages.length > 0 && hasActiveAsyncTask(s.messages[s.messages.length - 1])
    if (hasActiveAsyncTaskInLastMsg) {
      // 有活跃任务，不重发，等待轮询恢复
      return
    }

    // 1) 末尾消息的续传判断
    const last = s.messages[s.messages.length - 1]
    let needResumeSend = false
    let resumeText: string | null = null
    let dropLastAssistant = false
    if (last.role === 'user') {
      // 末尾是 user(助手还没回)→ 重发
      patchActiveMessages(msgs => msgs.filter(m => !(m.role === 'assistant' && !m.content && !m.draft)))
      needResumeSend = true
      resumeText = last.content
    } else if (last.role === 'assistant' && (last.incomplete || (!last.content && !last.draft))) {
      // 末尾是未完成 assistant(incomplete=流中断有半截内容),或空占位 assistant(中断时一个 token 都没收到)。
      // 两种都要移除它 + 重发前一条 user。
      // patchActiveMessages 已同步更新 sessionsRef,但 send 构造 wire 时仍传 dropLastAssistant=true,
      // 作为防御性兜底,确保任何未同步的中间状态都不会把不完整 assistant 发回后端。
      patchActiveMessages(msgs => msgs.slice(0, -1))
      const prevUser = s.messages[s.messages.length - 2]
      if (prevUser?.role === 'user') {
        needResumeSend = true
        resumeText = prevUser.content
        dropLastAssistant = true
      }
    }

    if (needResumeSend && resumeText) {
      void sendRef.current(resumeText, { resume: true, dropLastAssistant })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId])

  const confirmDraftMsg = useCallback(async (msgId: string) => {
    const s = sessions.find(x => x.id === activeId)
    const msg = s?.messages.find(m => m.id === msgId)
    if (!msg?.draft) return
    // 防连点:status 已是 confirming/rejecting 时直接返回(按钮 disable 依赖 re-render,有窗口期)。
    if (msg.draft.status === 'confirming' || msg.draft.status === 'rejecting') return
    patchMessage(msgId, m => m.draft ? { ...m, draft: { ...m.draft, status: 'confirming', errorMessage: undefined } } : m)
    try {
      const { upscaledUrl, targetResolution, algorithm } = await confirmDraft(msg.draft.draftId)
      patchMessage(msgId, m => m.draft
        ? { ...m, draft: { ...m.draft, status: 'confirmed', resultDataUrl: upscaledUrl, errorMessage: undefined } }
        : m)
      void algorithm
      void targetResolution
    } catch (e) {
      const code = e instanceof Error ? e.message : '确认失败'
      const expired = code.includes('expired') || code.includes('not_found')
      patchMessage(msgId, m => m.draft
        ? { ...m, draft: { ...m.draft, status: expired ? 'expired' : 'error', errorMessage: code } }
        : m)
    }
  }, [sessions, activeId, patchMessage])

  const rejectDraftMsg = useCallback(async (msgId: string) => {
    const s = sessions.find(x => x.id === activeId)
    const msg = s?.messages.find(m => m.id === msgId)
    if (!msg?.draft) return
    if (msg.draft.status === 'confirming' || msg.draft.status === 'rejecting') return
    patchMessage(msgId, m => m.draft ? { ...m, draft: { ...m.draft, status: 'rejecting', errorMessage: undefined } } : m)
    try {
      const { newDraftId, previewUrl } = await rejectDraft(msg.draft.draftId)
      // 更新为新草稿,重置状态 + 重新拉预览
      patchMessage(msgId, m => m.draft
        ? { ...m, draft: { ...m.draft, draftId: newDraftId, previewUrl, status: 'pending', previewDataUrl: undefined, resultDataUrl: undefined, errorMessage: undefined } }
        : m)
      try {
        const { previewDataUrl } = await getDraftPreview(newDraftId)
        patchMessage(msgId, m => m.draft
          ? { ...m, draft: { ...m.draft, previewDataUrl } }
          : m)
      } catch (e) {
        const code = e instanceof Error ? e.message : '预览加载失败'
        patchMessage(msgId, m => m.draft
          ? { ...m, draft: { ...m.draft, status: 'error', errorMessage: code } }
          : m)
      }
    } catch (e) {
      const code = e instanceof Error ? e.message : '重新生成失败'
      const expired = code.includes('expired') || code.includes('not_found')
      patchMessage(msgId, m => m.draft
        ? { ...m, draft: { ...m.draft, status: expired ? 'expired' : 'error', errorMessage: code } }
        : m)
    }
  }, [sessions, activeId, patchMessage])

  /** 轮询视频任务状态，完成后更新消息内容。 */
  const pollVideoStatus = useCallback(async (videoId: string, msgId: string) => {
    if (pollingVideoIds.has(videoId)) return
    pollingVideoIds.add(videoId)

    let attempts = 0
    while (attempts < VIDEO_POLL_MAX_ATTEMPTS) {
      attempts++
      await new Promise(resolve => setTimeout(resolve, VIDEO_POLL_INTERVAL_MS))

      try {
        const status: VideoStatusResponse = await getVideoStatus(videoId)
        const terminalStatus = isVideoTerminal(status.status)

        if (terminalStatus) {
          pollingVideoIds.delete(videoId)
          const video = status.video
          if (status.status === 'succeeded' && video?.url) {
            // 视频生成成功，更新消息内容
            patchMessage(msgId, m => ({
              ...m,
              content: `Video generated successfully. URL: ${video.url}`,
              intent: 'generation:video',
              model: 'video',
            }))
          } else if (status.status === 'failed' || status.status === 'error') {
            // 视频生成失败
            const errorMsg = status.error?.message || '视频生成失败'
            patchMessage(msgId, m => ({
              ...m,
              content: `Video generation failed: ${errorMsg}`,
              error: true,
            }))
          }
          break
        }

        // 仍在进行中，继续轮询
      } catch (e) {
        // 网络错误或 API 调用失败，继续重试
        console.warn(`Failed to poll video status for ${videoId}:`, e)
      }
    }

    pollingVideoIds.delete(videoId)
  }, [patchMessage])

  /** 刷新后自动轮询未完成的视频任务。 */
  useEffect(() => {
    if (!activeId) return

    const s = sessionsRef.current.find(x => x.id === activeId)
    if (!s) return

    // 查找所有有活跃视频任务的助手消息
    const videoMessages = s.messages.filter(
      m => m.role === 'assistant' && m.videoId && !m.error && !m.incomplete
    )

    videoMessages.forEach(msg => {
      if (msg.videoId) {
        pollVideoStatus(msg.videoId, msg.id)
      }
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId, resumePollingKey, pollVideoStatus])

  /** 组件卸载时清理所有轮询。 */
  useEffect(() => {
    return () => {
      clearAllPolling()
    }
  }, [])

  const active = sessions.find(s => s.id === activeId) ?? null

  return {
    sessions, activeId, active, streaming, error, pendingAssistantId,
    newSession, selectSession, deleteSession,
    send, stop, clearActive,
    confirmDraftMsg, rejectDraftMsg,
  }
}
