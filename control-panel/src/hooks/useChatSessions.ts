import { useCallback, useEffect, useRef } from 'react'
import { requestChatCompletion } from '@/api/consoleChat'
import type { ChatReferenceImage, GenerationOptions } from '@/types'
import {
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
  describeDraftFailure,
  newSessionId,
  nextMessageId,
  pollDraftUntilSettled,
  pollDraftProgressUntilStopped,
  pollVideoUntilTerminal,
  resumedSessionIds,
} from '@/services/chatRuntime'

function hasActiveAsyncTask(msg: ChatPageMessage): boolean {
  if (msg.videoId && !msg.error && !msg.incomplete) return true
  if (msg.draft && ['queued', 'running', 'generating', 'pending', 'refining', 'confirming', 'rejecting'].includes(msg.draft.status)) return true
  return false
}

const CHAT_DRAFT_STATUSES = new Set<ChatDraftState['status']>([
  'queued',
  'running',
  'generating',
  'pending',
  'refining',
  'confirming',
  'completed',
  'confirmed',
  'rejecting',
  'rejected',
  'cancelled',
  'expired',
  'error',
])

function normalizeDraftStatus(status: string | undefined): ChatDraftState['status'] {
  if (status && CHAT_DRAFT_STATUSES.has(status as ChatDraftState['status'])) {
    return status as ChatDraftState['status']
  }
  return 'running'
}

function normalizeDraftProgress(progress: number | undefined): number | undefined {
  if (typeof progress !== 'number' || Number.isNaN(progress)) return undefined
  return Math.min(1, Math.max(0, progress))
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
  send: (
    text: string,
    opts?: {
      generationOptions?: GenerationOptions
      referenceImage?: ChatReferenceImage
    },
  ) => Promise<void>
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

  const abortRef = useRef<AbortController | null>(null)
  const inflightRef = useRef(false)
  const pendingAssistantIdRef = useRef<string | null>(null)
  const resumeSessionRef = useRef<string | null>(null)
  const sessionsRef = useRef<ChatSession[]>(sessions)
  const flushRetryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const streamingRef = useRef(false)

  useEffect(() => { sessionsRef.current = sessions }, [sessions])
  useEffect(() => { streamingRef.current = streaming }, [streaming])

  const setPendingAssistantId = useCallback((id: string | null) => {
    pendingAssistantIdRef.current = id
    setPendingAssistantIdState(id)
  }, [setPendingAssistantIdState])

  useEffect(() => {
    if (sessions.length === 0) {
      const now = Date.now()
      const s: ChatSession = { id: newSessionId(), title: '新对话', messages: [], createdAt: now, updatedAt: now }
      setSessions([s])
      setActiveId(s.id)
    } else if (!activeId || !sessions.some(s => s.id === activeId)) {
      setActiveId(sessions[0].id)
    }
  }, [sessions, activeId, setSessions, setActiveId])

  useEffect(() => {
    return () => {
      const rs = resumeSessionRef.current
      if (rs) {
        resumedSessionIds.delete(rs)
        resumeSessionRef.current = null
      }
      abortRef.current?.abort()
      abortRef.current = null
      inflightRef.current = false
      if (flushRetryTimerRef.current) clearTimeout(flushRetryTimerRef.current)
    }
  }, [])

  useEffect(() => {
    setResumePollingKey(prev => prev + 1)
  }, [activeId, setResumePollingKey])

  const flushToStorage = useCallback((retryCount = 0) => {
    try {
      persistSessions(sessionsRef.current)
      if (flushRetryTimerRef.current) {
        clearTimeout(flushRetryTimerRef.current)
        flushRetryTimerRef.current = null
      }
    } catch (e) {
      if (retryCount < 3) {
        flushRetryTimerRef.current = setTimeout(
          () => flushToStorage(retryCount + 1),
          100 * Math.pow(2, retryCount),
        )
      } else {
        setError(`草稿保存失败: ${e instanceof Error ? e.message : 'unknown'}`)
      }
    }
  }, [setError])

  useEffect(() => {
    const t = setTimeout(() => {
      try { persistSessions(sessions) } catch { /* ignore */ }
    }, 500)
    return () => clearTimeout(t)
  }, [sessions])

  useEffect(() => {
    const flush = () => {
      try {
        let toFlush = sessionsRef.current
        if (streamingRef.current) {
          toFlush = toFlush.map(s => {
            const last = s.messages[s.messages.length - 1]
            if (last?.role === 'assistant' && last.content && !last.incomplete && !last.draft) {
              return { ...s, messages: s.messages.slice(0, -1).concat({ ...last, incomplete: true }) }
            }
            return s
          })
        }
        persistSessions(toFlush)
      } catch { /* ignore */ }
    }
    window.addEventListener('pagehide', flush)
    return () => window.removeEventListener('pagehide', flush)
  }, [])

  useEffect(() => {
    if (activeId) {
      try { persistActiveId(activeId) } catch { /* ignore */ }
    }
  }, [activeId])

  const patchSessions = useCallback((updater: (base: ChatSession[]) => ChatSession[]) => {
    const next = updater(sessionsRef.current)
    sessionsRef.current = next
    setSessions(next)
  }, [setSessions])

  const patchActiveMessages = useCallback((updater: (msgs: ChatPageMessage[]) => ChatPageMessage[]) => {
    patchSessions(base => base.map(s => {
      if (s.id !== activeId) return s
      const messages = updater(s.messages)
      const title = s.title === '新对话' && messages.some(m => m.role === 'user')
        ? titleFromMessages(messages)
        : s.title
      return { ...s, messages, title, updatedAt: Date.now() }
    }))
  }, [activeId, patchSessions])

  const patchMessage = useCallback((msgId: string, updater: (m: ChatPageMessage) => ChatPageMessage) => {
    patchActiveMessages(msgs => msgs.map(m => (m.id === msgId ? updater(m) : m)))
  }, [patchActiveMessages])

  const stop = useCallback(() => {
    abortRef.current?.abort()
    abortRef.current = null
    inflightRef.current = false
    resumeSessionRef.current = null
    setStreaming(false)
  }, [setStreaming])

  const newSession = useCallback(() => {
    if (streaming) stop()
    const now = Date.now()
    const s: ChatSession = { id: newSessionId(), title: '新对话', messages: [], createdAt: now, updatedAt: now }
    patchSessions(prev => [s, ...prev])
    setActiveId(s.id)
    setError(null)
  }, [streaming, stop, patchSessions, setActiveId, setError])

  const selectSession = useCallback((id: string) => {
    if (streaming) stop()
    setActiveId(id)
    setError(null)
  }, [streaming, stop, setActiveId, setError])

  const deleteSession = useCallback(async (id: string) => {
    if (id === activeId) stop()
    void deleteSessionDrafts(id).catch((e) => {
      console.warn('删除会话草稿失败:', e instanceof Error ? e.message : e)
    })
    patchSessions(prev => {
      const next = prev.filter(s => s.id !== id)
      if (id === activeId) {
        if (next.length > 0) setActiveId(next[0].id)
        else {
          const now = Date.now()
          const fresh: ChatSession = { id: newSessionId(), title: '新对话', messages: [], createdAt: now, updatedAt: now }
          setActiveId(fresh.id)
          return [fresh]
        }
      }
      return next
    })
  }, [activeId, stop, patchSessions, setActiveId])

  const clearActive = useCallback(() => {
    stop()
    if (activeId) resumedSessionIds.delete(activeId)
    patchActiveMessages(() => [])
  }, [stop, activeId, patchActiveMessages])

  const pollDraftPreview = useCallback(async (draftId: string, msgId: string) => {
    const result = await pollDraftUntilSettled(draftId, progress => {
      patchMessage(msgId, message => {
        if (message.draft?.draftId !== draftId) return message
        return {
          ...message,
          draft: {
            ...message.draft,
            status: normalizeDraftStatus(progress.status),
            stage: progress.stage ?? message.draft.stage,
            progress: normalizeDraftProgress(progress.progress) ?? message.draft.progress,
            progressSource: progress.progressSource ?? message.draft.progressSource,
            errorMessage: undefined,
          },
          awaitingDraft: true,
        }
      })
    })
    if (result.kind === 'duplicate') return
    patchMessage(msgId, message => {
      if (message.draft?.draftId !== draftId) return message
      if (result.kind === 'ready') {
        return {
          ...message,
          draft: {
            ...message.draft,
            status: 'pending',
            stage: 'preview_ready',
            progress: 1,
            progressSource: 'complete',
            previewDataUrl: result.previewDataUrl,
            errorMessage: undefined,
          },
          awaitingDraft: false,
        }
      }
      return {
        ...message,
        draft: { ...message.draft, status: result.kind, errorMessage: result.message },
        awaitingDraft: false,
      }
    })
    flushToStorage()
  }, [patchMessage, flushToStorage])

  const send = useCallback(async (
    text: string,
    opts?: {
      resume?: boolean
      dropLastAssistant?: boolean
      generationOptions?: GenerationOptions
      referenceImage?: ChatReferenceImage
    },
  ) => {
    const trimmed = text.trim()
    if (!trimmed || streaming || inflightRef.current) return
    inflightRef.current = true
    const isResume = Boolean(opts?.resume)
    setError(null)
    if (!isResume && activeId) resumedSessionIds.delete(activeId)

    const userMsg: ChatPageMessage = {
      id: nextMessageId(),
      role: 'user',
      content: trimmed,
      referenceImageDataUrl: opts?.referenceImage?.dataUrl,
      referenceImageName: opts?.referenceImage?.name,
      ts: Date.now(),
    }
    const assistantId = nextMessageId()
    const assistantMsg: ChatPageMessage = { id: assistantId, role: 'assistant', content: '', ts: Date.now() }

    // Snapshot history before mutating sessionsRef. patchActiveMessages updates the
    // ref synchronously, so reading it afterwards would include userMsg and then
    // append the same message again when building the wire payload.
    const cur = sessionsRef.current.find(x => x.id === activeId)
    let baseMsgs = cur?.messages ?? []
    if (isResume && baseMsgs.length > 10) baseMsgs = baseMsgs.slice(-10)
    if (opts?.dropLastAssistant && baseMsgs.length > 0 && baseMsgs[baseMsgs.length - 1].role === 'assistant') {
      baseMsgs = baseMsgs.slice(0, -1)
    }
    const wireMessages: ChatMessage[] = (isResume ? [...baseMsgs] : [...baseMsgs, userMsg])
      .filter(m => m.role === 'user' || (m.role === 'assistant' && m.content && !m.draft))
      .map(m => ({
        role: m.role,
        content: m.referenceImageDataUrl
          ? [
              { type: 'text' as const, text: m.content },
              {
                type: 'image_url' as const,
                image_url: { url: m.referenceImageDataUrl },
              },
            ]
          : m.content,
      }))

    patchActiveMessages(msgs => isResume ? [...msgs, assistantMsg] : [...msgs, userMsg, assistantMsg])
    setPendingAssistantId(assistantId)

    setStreaming(true)
    const controller = new AbortController()
    abortRef.current = controller
    if (isResume) resumeSessionRef.current = activeId

    try {
      const resp = await requestChatCompletion(
        {
          model: 'auto',
          messages: wireMessages,
          stream: true,
          chat_session_id: activeId ?? undefined,
          generation_options: opts?.generationOptions,
        },
        controller.signal,
      )

      if (resp.kind === 'draft') {
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
          awaitingDraft: true,
          awaitingDraftSince: Date.now(),
        }))
        setPendingAssistantId(null)
        flushToStorage()
        setStreaming(false)
        abortRef.current = null
        inflightRef.current = false
        void pollDraftPreview(resp.draftId, assistantId)
        return
      }

      await consumeChatEventStream(resp.body, chunk => {
        const delta = chunk.choices?.[0]?.delta
        const meta = chunk._meta?.routed_to
        const streamError = chunk.error
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
          if (streamError) {
            next.error = true
            if (!next.content) next.content = errorMessage
          }
          return next
        })
        if (streamError) {
          setError(errorMessage)
          setPendingAssistantId(null)
        }
      })
      setStreaming(false)
      setPendingAssistantId(null)
      flushToStorage()
    } catch (e) {
      if (controller.signal.aborted) {
        patchMessage(assistantId, m => (m.content ? { ...m, incomplete: true } : m))
      } else {
        const msg = e instanceof Error ? e.message : '请求失败'
        setError(msg)
        patchActiveMessages(msgs => msgs.filter(m => !(m.id === assistantId && m.content === '' && !m.draft)))
      }
      setStreaming(false)
      setPendingAssistantId(null)
      flushToStorage()
    } finally {
      if (abortRef.current === controller) {
        abortRef.current = null
        inflightRef.current = false
        if (isResume) resumeSessionRef.current = null
      }
    }
  }, [streaming, activeId, patchActiveMessages, patchMessage, pollDraftPreview, setError, setPendingAssistantId, setStreaming, flushToStorage])

  const sendRef = useRef(send)
  useEffect(() => { sendRef.current = send }, [send])

  useEffect(() => {
    if (!activeId || resumedSessionIds.has(activeId)) return
    const s = sessionsRef.current.find(x => x.id === activeId)
    if (!s || s.messages.length === 0) return
    resumedSessionIds.add(activeId)

    for (const m of s.messages) {
      if (m.role !== 'assistant' || !m.draft) continue
      if (m.awaitingDraft && m.awaitingDraftSince && (Date.now() - m.awaitingDraftSince > 30000)) {
        patchMessage(m.id, mm => ({ ...mm, awaitingDraft: false }))
      }
      const st = m.draft.status
      if (st === 'generating' || m.awaitingDraft || st === 'pending' || st === 'confirming' || st === 'rejecting') {
        if (st !== 'generating') {
          patchMessage(m.id, mm => mm.draft ? { ...mm, draft: { ...mm.draft, status: 'pending', errorMessage: undefined } } : mm)
        }
        if (!m.draft.previewDataUrl) void pollDraftPreview(m.draft.draftId, m.id)
      } else if (st === 'confirmed') {
        if (!m.draft.resultDataUrl) {
          void getDraftResult(m.draft.draftId).then(
            ({ resultDataUrl }) => patchMessage(m.id, mm => mm.draft ? { ...mm, draft: { ...mm.draft, resultDataUrl } } : mm),
            (e: unknown) => patchMessage(m.id, mm => mm.draft ? { ...mm, draft: { ...mm.draft, resultLost: true, errorMessage: e instanceof Error ? e.message : '结果加载失败' } } : mm),
          )
        }
        if (!m.draft.previewDataUrl) void pollDraftPreview(m.draft.draftId, m.id)
      }
    }

    if (s.messages.some(hasActiveAsyncTask)) return
    const last = s.messages[s.messages.length - 1]
    let needResumeSend = false
    let resumeText: string | null = null
    let dropLastAssistant = false
    if (last.role === 'user') {
      patchActiveMessages(msgs => msgs.filter(m => !(m.role === 'assistant' && !m.content && !m.draft)))
      needResumeSend = true
      resumeText = last.content
    } else if (last.role === 'assistant' && (last.incomplete || (!last.content && !last.draft))) {
      if (last.awaitingDraft || last.draft?.status === 'generating') return
      if (!last.content && !last.draft && pendingAssistantIdRef.current === last.id) return
      patchActiveMessages(msgs => msgs.slice(0, -1))
      const prevUser = s.messages[s.messages.length - 2]
      if (prevUser?.role === 'user') {
        needResumeSend = true
        resumeText = prevUser.content
        dropLastAssistant = true
      }
    }
    if (needResumeSend && resumeText) void sendRef.current(resumeText, { resume: true, dropLastAssistant })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId])

  const pollVideoStatus = useCallback(async (videoId: string, msgId: string) => {
    const status = await pollVideoUntilTerminal(videoId)
    if (!status) return
    const resolvedUrl = status.video?.url || status.url
    if ((status.status === 'succeeded' || status.status === 'completed') && resolvedUrl) {
      patchMessage(msgId, message => ({ ...message, videoUrl: resolvedUrl, intent: 'generation:video', model: 'video' }))
      flushToStorage()
    } else if (status.status === 'failed' || status.status === 'error') {
      patchMessage(msgId, message => ({ ...message, content: `Video generation failed: ${status.error?.message || '视频生成失败'}`, error: true }))
      flushToStorage()
    }
  }, [patchMessage, flushToStorage])

  const confirmDraftMsg = useCallback(async (msgId: string) => {
    const s = sessions.find(x => x.id === activeId)
    const msg = s?.messages.find(m => m.id === msgId)
    if (!msg?.draft || msg.draft.status === 'confirming' || msg.draft.status === 'rejecting') return
    patchMessage(msgId, m => m.draft ? { ...m, draft: { ...m.draft, status: 'confirming', errorMessage: undefined } } : m)
    flushToStorage()
    const progressController = new AbortController()
    const progressPolling = pollDraftProgressUntilStopped(
      msg.draft.draftId,
      progressController.signal,
      progress => {
        if (!['queued', 'running', 'generating', 'refining', 'confirming'].includes(progress.status ?? '')) return
        patchMessage(msgId, message => message.draft ? {
          ...message,
          draft: {
            ...message.draft,
            status: normalizeDraftStatus(progress.status),
            stage: progress.stage ?? message.draft.stage,
            progress: normalizeDraftProgress(progress.progress) ?? message.draft.progress,
            progressSource: progress.progressSource ?? message.draft.progressSource,
          },
        } : message)
      },
    )
    try {
      const result = await confirmDraft(msg.draft.draftId)
      if ('videoId' in result) {
        patchMessage(msgId, m => ({ ...m, draft: undefined, videoId: result.videoId, intent: 'generation:video', model: 'video' }))
        flushToStorage()
        void pollVideoStatus(result.videoId, msgId)
      } else {
        patchMessage(msgId, m => m.draft ? {
          ...m,
          intent: result.mediaType === 'video' ? 'generation:video' : m.intent,
          model: result.mediaType === 'video' ? 'comfyui' : m.model,
          draft: { ...m.draft, mediaType: result.mediaType, status: 'confirmed', resultDataUrl: result.upscaledUrl, errorMessage: undefined },
        } : m)
        flushToStorage()
      }
    } catch (e) {
      const code = e instanceof Error ? e.message : '确认失败'
      const expired = code.includes('expired') || code.includes('not_found')
      patchMessage(msgId, m => m.draft ? { ...m, draft: { ...m.draft, status: expired ? 'expired' : 'error', errorMessage: code.includes('upstream_unavailable') ? '视频生成上游暂时不可用,请稍后重试' : describeDraftFailure(code) } } : m)
      flushToStorage()
    } finally {
      progressController.abort()
      await progressPolling
    }
  }, [sessions, activeId, patchMessage, pollVideoStatus, flushToStorage])

  const rejectDraftMsg = useCallback(async (msgId: string) => {
    const s = sessions.find(x => x.id === activeId)
    const msg = s?.messages.find(m => m.id === msgId)
    if (!msg?.draft || msg.draft.status === 'confirming' || msg.draft.status === 'rejecting') return
    patchMessage(msgId, m => m.draft ? { ...m, draft: { ...m.draft, status: 'rejecting', errorMessage: undefined } } : m)
    flushToStorage()
    try {
      const { newDraftId, previewUrl } = await rejectDraft(msg.draft.draftId)
      patchMessage(msgId, m => m.draft ? { ...m, draft: { ...m.draft, draftId: newDraftId, previewUrl, status: 'generating', previewDataUrl: undefined, resultDataUrl: undefined, errorMessage: undefined }, awaitingDraft: true, awaitingDraftSince: Date.now() } : m)
      flushToStorage()
      void pollDraftPreview(newDraftId, msgId)
    } catch (e) {
      const code = e instanceof Error ? e.message : '重新生成失败'
      const expired = code.includes('expired') || code.includes('not_found')
      patchMessage(msgId, m => m.draft ? { ...m, draft: { ...m.draft, status: expired ? 'expired' : 'error', errorMessage: code } } : m)
      flushToStorage()
    }
  }, [sessions, activeId, patchMessage, pollDraftPreview, flushToStorage])

  useEffect(() => {
    if (!activeId) return
    const s = sessionsRef.current.find(x => x.id === activeId)
    if (!s) return
    s.messages
      .filter(m => m.role === 'assistant' && m.videoId && !m.error && !m.incomplete)
      .forEach(msg => { if (msg.videoId) void pollVideoStatus(msg.videoId, msg.id) })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId, resumePollingKey, pollVideoStatus])

  useEffect(() => () => clearAllChatPolling(), [])

  const active = sessions.find(s => s.id === activeId) ?? null

  return {
    sessions, activeId, active, streaming, error, pendingAssistantId,
    newSession, selectSession, deleteSession,
    send, stop, clearActive,
    confirmDraftMsg, rejectDraftMsg,
  }
}
