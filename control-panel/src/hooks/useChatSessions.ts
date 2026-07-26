import { useCallback, useEffect, useRef } from 'react'
import {
  requestChatCompletion,
  getDraftResult,
  confirmDraft,
  rejectDraft,
  deleteSessionDrafts,
} from '@/api/client'
import type { ChatPageMessage, ChatMessage, ChatSession, ChatDraftState } from '@/types'
import { useChatStore } from '@/stores/chatStore'
import {
  persistActiveId,
  persistSessions,
  titleFromMessages,
} from '@/services/chatStorage'
import {
  clearAllChatPolling,
  consumeChatEventStream,
  newSessionId,
  nextMessageId,
  pollDraftUntilSettled,
  pollVideoUntilTerminal,
  resumedSessionIds,
} from '@/services/chatRuntime'

/** 判断消息是否包含活跃的异步任务（视频/草稿）。 */
function hasActiveAsyncTask(msg: ChatPageMessage): boolean {
  // 视频任务：有 videoId 且未标记 error/incomplete
  if (msg.videoId && !msg.error && !msg.incomplete) return true
  // 草稿任务：generating(后台生成中)/pending/confirming/rejecting 状态
  if (msg.draft && ['generating', 'pending', 'confirming', 'rejecting'].includes(msg.draft.status)) return true
  return false
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
  const sessions = useChatStore(state => state.sessions)
  const activeId = useChatStore(state => state.activeId)
  const streaming = useChatStore(state => state.streaming)
  const error = useChatStore(state => state.error)
  const pendingAssistantId = useChatStore(state => state.pendingAssistantId)
  const resumePollingKey = useChatStore(state => state.resumePollingKey)
  const setSessions = useChatStore(state => state.setSessions)
  const setActiveId = useChatStore(state => state.setActiveId)
  const setStreaming = useChatStore(state => state.setStreaming)
  const setError = useChatStore(state => state.setError)
  const setPendingAssistantIdState = useChatStore(state => state.setPendingAssistantId)
  const setResumePollingKey = useChatStore(state => state.setResumePollingKey)
  // 当前正在等待回复的助手消息 ID(空 content 占位)。用于在切换会话后仍能在
  // 原会话上显示三点动画——streaming=false 不代表该消息不需要提示。
  // ref + state 双轨:ref 供 send/resume 同步读取(避免状态异步更新导致的孤儿窗口——
  // 切换会话频繁时 state 还没 flush,读到的旧 id 已对应被 slice 掉的占位,三点闪烁/失效);
  // state 供 MessageBubble re-render。两者始终同步写入。
  const pendingAssistantIdRef = useRef<string | null>(null)
  const setPendingAssistantId = useCallback((id: string | null) => {
    pendingAssistantIdRef.current = id
    setPendingAssistantIdState(id)
  }, [])
  const abortRef = useRef<AbortController | null>(null)
  const inflightRef = useRef(false)
  // 用于触发轮询恢复：每次 activeId 变化时递增，确保刷新/切换会话后轮询 effect 重新运行
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
        persistSessions(sessions)
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
   *
   * 带指数退避重试：localStorage 配额超限或序列化失败时最多重试 3 次，
   * 全部失败后通过 setError 通知用户。
   */
  const flushRetryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => {
    return () => {
      if (flushRetryTimerRef.current) {
        clearTimeout(flushRetryTimerRef.current)
        flushRetryTimerRef.current = null
      }
    }
  }, [])

  const flushToStorage = useCallback((retryCount = 0) => {
    const MAX_RETRIES = 3
    try {
      persistSessions(sessionsRef.current)
      // 成功则清除重试定时器
      if (flushRetryTimerRef.current) {
        clearTimeout(flushRetryTimerRef.current)
        flushRetryTimerRef.current = null
      }
    } catch (e) {
      if (retryCount < MAX_RETRIES) {
        flushRetryTimerRef.current = setTimeout(
          () => flushToStorage(retryCount + 1),
          100 * Math.pow(2, retryCount),
        )
      } else {
        setError(`草稿保存失败: ${e instanceof Error ? e.message : 'unknown'}`)
      }
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
        persistSessions(toFlush)
      } catch {
        // ignore
      }
    }
    window.addEventListener('pagehide', flush)
    return () => window.removeEventListener('pagehide', flush)
  }, [])

  useEffect(() => {
    if (activeId) {
      try { persistActiveId(activeId) } catch { /* ignore */ }
    }
  }, [activeId])

  const patchActiveMessages = useCallback(
    (updater: (msgs: ChatPageMessage[]) => ChatPageMessage[]) => {
      const base = sessionsRef.current
      const next = base.map(s => {
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
      setSessions(next)
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

  /** 轮询草稿预览图，直到就绪/失败/超时。
   *
   * 后台 ComfyUI 生成是异步的:preview 端点先返回 202(generating),生成完成后 200。
   * runtime service 负责请求循环、重试和去重；本函数只把终态写回 store。
   *
   * 终态处理:
   * - 200 + previewDataUrl → status='pending' + previewDataUrl + awaitingDraft=false
   * - 4xx(not_found/expired/draft_failed) → status='expired'/'error'
   * - 超时 → status='expired' + errorMessage
  */
  const pollDraftPreview = useCallback(async (draftId: string, msgId: string) => {
    // 防御性守卫:patch 前确认消息当前 draftId 仍是本轮轮询的 draftId。
    // reject 会替换 draftId 并起新轮询;若旧轮询仍 in-flight,其 patch 不应
    // 覆盖新草稿的 generating 状态(返回 m 即 no-op)。
    const owns = (m: ChatPageMessage): m is ChatPageMessage & { draft: ChatDraftState } =>
      m.draft?.draftId === draftId

    const result = await pollDraftUntilSettled(draftId)
    if (result.kind === 'duplicate') return
    patchMessage(msgId, message => {
      if (!owns(message)) return message
      if (result.kind === 'ready') {
        return {
          ...message,
          draft: {
            ...message.draft,
            status: 'pending',
            previewDataUrl: result.previewDataUrl,
            errorMessage: undefined,
          },
          awaitingDraft: false,
        }
      }
      return {
        ...message,
        draft: {
          ...message.draft,
          status: result.kind,
          errorMessage: result.message,
        },
        awaitingDraft: false,
      }
    })
    flushToStorage()
  }, [patchMessage, flushToStorage])

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
      id: nextMessageId(), role: 'user', content: trimmed, ts: Date.now(),
    }
    const assistantId = nextMessageId()
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
    const finishCurrentStream = () => {
      setStreaming(false)
      setPendingAssistantId(null)
      flushToStorage()
    }

    try {
      const resp = await requestChatCompletion(
        { model: 'auto', messages: wireMessages, stream: true, chat_session_id: activeId ?? undefined },
        controller.signal,
      )

      if (resp.kind === 'draft') {
        // 草稿分支:dispatcher 立即返回 draft_id(后台 ComfyUI 异步生成预览)。
        // 把 assistant 占位转为草稿消息,初始 status='generating',启动轮询拉预览。
        const draft: ChatDraftState = {
          draftId: resp.draftId,
          previewUrl: resp.previewUrl,
          mediaType: resp.mediaType,
          status: 'generating',
        }
        patchMessage(assistantId, m => ({
          ...m,
          intent: resp.mediaType === 'image' ? 'generation:image' : 'generation:video',
          model: 'draft',
          draft,
          awaitingDraft: true,       // 标记 server-side draft task 进行中，防止刷新误续传
          awaitingDraftSince: Date.now(),
        }))
        // 草稿消息渲染 DraftCard(非 text 分支),不再需要三点占位
        setPendingAssistantId(null)
        // 关键状态转换: draft 响应到达，立即落盘防止 debounce 窗口期内刷新导致重发
        flushToStorage()
        setStreaming(false)
        abortRef.current = null
        inflightRef.current = false  // 主请求已完成,预览轮询是 best-effort,不应阻塞下一条 send
        // 异步轮询预览图(202=generating 继续轮询;200=就绪;4xx=expired/failed)
        void pollDraftPreview(resp.draftId, assistantId)
        return
      }

      // 流式 I/O 与 SSE 解帧由 runtime service 负责；hook 只处理业务状态转换。
      await consumeChatEventStream(resp.body, chunk => {
        const delta = chunk.choices?.[0]?.delta
        const meta = chunk._meta?.routed_to
        const streamError = chunk.error
        const isErr = Boolean(streamError)
        const errorMessage = streamError?.message ?? streamError?.code ?? '请求失败'
        patchMessage(assistantId, message => {
          const next: ChatPageMessage = { ...message }
          if (delta?.content) {
            next.content += delta.content
            setPendingAssistantId(null)
          }
          if (meta?.intent && !next.intent) next.intent = meta.intent
          if (meta?.model && !next.model) next.model = meta.model
          const videoId = chunk._meta?.video_id
          if (videoId && !next.videoId) next.videoId = videoId
          if (isErr) {
            next.error = true
            if (!next.content) next.content = errorMessage
          }
          return next
        })
        if (isErr) {
          setError(errorMessage)
          setPendingAssistantId(null)
        }
      })
      finishCurrentStream()
    } catch (e) {
      if (controller.signal.aborted) {
        // 标记 incomplete(刷新续传依据)。
        patchMessage(assistantId, m => (m.content ? { ...m, incomplete: true } : m))
        setStreaming(false)
        // 中断时一个 token 都没收到(空占位):必须清 pendingAssistantId。
        // 否则切走再切回,resume effect(line 741)误判"send 仍在进行中"而提前 return,
        // 既不续传也不清三点 → TypingDots 永久卡死(Issue 3 回归)。
        // stop() 注释说"不清 pendingAssistantId"是针对正常流式中途切走(切回时三点仍该显示),
        // 但那是在 send 协程仍存活、streaming=true 的前提下;这里 send 已终止,性质不同。
        const stalled = sessionsRef.current
          .find(s => s.id === activeId)?.messages
          .find(m => m.id === assistantId)
        // 所有权守卫:仅当 pending 仍指向本 send 的占位时才清,避免覆盖已起步的新 send B
        // (虽 microtask-vs-useEffect 时序下不可达,但防御未来 send 启动时机变更)。
        if (stalled && !stalled.content && !stalled.draft && pendingAssistantIdRef.current === assistantId) {
          setPendingAssistantId(null)
        }
      } else {
        const msg = e instanceof Error ? e.message : '请求失败'
        setError(msg)
        setPendingAssistantId(null)
        // 移除空占位
        patchActiveMessages(msgs => msgs.filter(m => !(m.id === assistantId && m.content === '' && !m.draft)))
        setStreaming(false)
        flushToStorage()
      }
    } finally {
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
  }, [streaming, activeId, patchActiveMessages, patchMessage, pollDraftPreview, setPendingAssistantId, flushToStorage])

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

    // 2) 恢复所有草稿消息的预览/结果(data URL 不持久化,刷新后全丢)
    //    - generating / awaitingDraft(server-side 任务进行中) → 只确保有轮询在跑,
    //      不 wipe previewDataUrl、不改 status(pollDraftPreview 的 Set 去重保证幂等)。
    //    - pending 且无 previewDataUrl → 启动 pollDraftPreview(后端已生成完,首轮即 200)。
    //    - confirmed 且无 resultDataUrl → getDraftResult(后端 result.bin 已持久化)。
    //    - error/expired → 不动。
    //    同时重置超时的 awaitingDraft 标记(>30s)，防止永久阻塞续传。
    for (const m of s.messages) {
      if (m.role !== 'assistant' || !m.draft) continue
      // 重置过期的 awaitingDraft 标记
      if (m.awaitingDraft && m.awaitingDraftSince && (Date.now() - m.awaitingDraftSince > 30000)) {
        patchMessage(m.id, mm => ({ ...mm, awaitingDraft: false }))
      }
      const st = m.draft.status
      if (st === 'generating' || m.awaitingDraft) {
        // 后台生成中:确保轮询在跑(幂等),不 wipe 状态
        void pollDraftPreview(m.draft.draftId, m.id)
      } else if (st === 'pending' || st === 'confirming' || st === 'rejecting') {
        // 降级 pending,若预览丢失则轮询补拉(后端已生成,首轮 200)
        patchMessage(m.id, mm => mm.draft
          ? { ...mm, draft: { ...mm.draft, status: 'pending', errorMessage: undefined } }
          : mm)
        if (!m.draft.previewDataUrl) {
          void pollDraftPreview(m.draft.draftId, m.id)
        }
      } else if (st === 'confirmed') {
        // 高清图:后端文件存储已持久化,刷新后重取(不标记 resultLost)。
        if (!m.draft.resultDataUrl) {
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
        }
        // 已确认但预览也丢了(刷新),补拉预览(后端有 preview.bin)
        if (!m.draft.previewDataUrl) {
          void pollDraftPreview(m.draft.draftId, m.id)
        }
      }
      // error/expired:不动
    }

    // 检查是否有活跃的异步任务（视频/草稿），如果有则跳过续传
    // 扫描所有消息而非仅最后一条，防止 flushToStorage 静默失败时漏掉 draft 状态
    const anyActiveAsyncTask = s.messages.some(hasActiveAsyncTask)
    if (anyActiveAsyncTask) {
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
      // 如果 awaitingDraft 已设置，说明 server-side draft task 仍在进行中，不要续传。
      // 同时检查 draft.status==='generating'(awaitingDraft 30s 后会被重置,但生成
      // 可能仍未完成 —— 轮询窗口 120s),避免重置后误判中断而重发。
      if (last.awaitingDraft || last.draft?.status === 'generating') {
        return
      }
      // Issue 3: 若该空占位仍是当前 pending 的助手消息(pendingAssistantIdRef 同步读取),
      // 说明它的 send 仍在进行中(只是被 StrictMode/切换暂时打断观察),并非真正中断——
      // 不要 slice + 重发,否则会造成孤儿窗口(三点瞬时失效)+ 重复请求(Issue 2)。
      if (!last.content && !last.draft && pendingAssistantIdRef.current === last.id) {
        return
      }
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

  /** 轮询视频任务状态，完成后更新消息内容。 */
  const pollVideoStatus = useCallback(async (videoId: string, msgId: string) => {
    const status = await pollVideoUntilTerminal(videoId)
    if (!status) return
    const resolvedUrl = status.video?.url || status.url
    if ((status.status === 'succeeded' || status.status === 'completed') && resolvedUrl) {
      patchMessage(msgId, message => ({
        ...message,
        videoUrl: resolvedUrl,
        intent: 'generation:video',
        model: 'video',
      }))
      flushToStorage()
    } else if (status.status === 'failed' || status.status === 'error') {
      const errorMessage = status.error?.message || '视频生成失败'
      patchMessage(msgId, message => ({
        ...message,
        content: `Video generation failed: ${errorMessage}`,
        error: true,
      }))
      flushToStorage()
    }
  }, [patchMessage, flushToStorage])

  const confirmDraftMsg = useCallback(async (msgId: string) => {
    const s = sessions.find(x => x.id === activeId)
    const msg = s?.messages.find(m => m.id === msgId)
    if (!msg?.draft) return
    // 防连点:status 已是 confirming/rejecting 时直接返回(按钮 disable 依赖 re-render,有窗口期)。
    if (msg.draft.status === 'confirming' || msg.draft.status === 'rejecting') return
    patchMessage(msgId, m => m.draft ? { ...m, draft: { ...m.draft, status: 'confirming', errorMessage: undefined } } : m)
    flushToStorage()
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
        flushToStorage()
        void pollVideoStatus(result.videoId, msgId)
      } else {
        // 图片草稿确认:高清放大结果挂到 draft.resultDataUrl。
        patchMessage(msgId, m => m.draft
          ? { ...m, draft: { ...m.draft, status: 'confirmed', resultDataUrl: result.upscaledUrl, errorMessage: undefined } }
          : m)
        flushToStorage()
      }
    } catch (e) {
      const code = e instanceof Error ? e.message : '确认失败'
      const expired = code.includes('expired') || code.includes('not_found')
      // 上游瞬时不可用(Agnes /videos 502/503):提示用户可重试,而非笼统的"操作失败"。
      const friendly = code.includes('upstream_unavailable')
        ? '视频生成上游暂时不可用,请稍后重试'
        : code
      patchMessage(msgId, m => m.draft
        ? { ...m, draft: { ...m.draft, status: expired ? 'expired' : 'error', errorMessage: friendly } }
        : m)
      flushToStorage()
    }
  }, [sessions, activeId, patchMessage, pollVideoStatus, flushToStorage])

  const rejectDraftMsg = useCallback(async (msgId: string) => {
    const s = sessions.find(x => x.id === activeId)
    const msg = s?.messages.find(m => m.id === msgId)
    if (!msg?.draft) return
    if (msg.draft.status === 'confirming' || msg.draft.status === 'rejecting') return
    patchMessage(msgId, m => m.draft ? { ...m, draft: { ...m.draft, status: 'rejecting', errorMessage: undefined } } : m)
    flushToStorage()
    try {
      const { newDraftId, previewUrl } = await rejectDraft(msg.draft.draftId)
      // 后端异步生成新草稿:立即切到 generating,清旧预览,启动轮询(同 send 草稿分支)。
      // awaitingDraft 守卫阻止恢复效果误续传/误重发(Issue 2)。
      patchMessage(msgId, m => m.draft
        ? { ...m, draft: { ...m.draft, draftId: newDraftId, previewUrl, status: 'generating', previewDataUrl: undefined, resultDataUrl: undefined, errorMessage: undefined }, awaitingDraft: true, awaitingDraftSince: Date.now() }
        : m)
      flushToStorage()
      void pollDraftPreview(newDraftId, msgId)
    } catch (e) {
      const code = e instanceof Error ? e.message : '重新生成失败'
      const expired = code.includes('expired') || code.includes('not_found')
      patchMessage(msgId, m => m.draft
        ? { ...m, draft: { ...m.draft, status: expired ? 'expired' : 'error', errorMessage: code } }
        : m)
      flushToStorage()
    }
  }, [sessions, activeId, patchMessage, pollDraftPreview, flushToStorage])

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
      clearAllChatPolling()
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
