import { useCallback, useEffect, useRef } from 'react'
import { requestChatCompletion } from '@/api/consoleChat'
import { createVideoDraftFromSource } from '@/api/sourceDraftVideo'
import {
  cancelGenerationRequest,
  cancelGenerationRequestAndWait,
  newGenerationRequestId,
  waitForGenerationRequestDraft,
} from '@/api/generationRequest'
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

interface SourceAwareGenerationOptions extends GenerationOptions {
  source_draft_id?: string
}

interface ActiveGeneration {
  requestId: string
  sessionId: string
  assistantId: string
  controller: AbortController
}

function hasActiveAsyncTask(msg: ChatPageMessage): boolean {
  if (msg.videoId && !msg.error && !msg.incomplete) return true
  if (msg.awaitingDraft && msg.generationRequestId) return true
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
  if (status === 'failed') return 'error'
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
      generationOptions?: SourceAwareGenerationOptions
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
  const activeGenerationRef = useRef<ActiveGeneration | null>(null)
  const cancelledRequestIdsRef = useRef(new Set<string>())
  const inflightRef = useRef(false)
  const pendingAssistantIdRef = useRef<string | null>(null)
  const resumeSessionRef = useRef<string | null>(null)
  const sessionsRef = useRef<ChatSession[]>(sessions)
  const flushRetryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const streamingRef = useRef(false)
  const recoveryControllersRef = useRef(new Map<string, AbortController>())
  const cancellationControllersRef = useRef(new Map<string, AbortController>())

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

  const detachTransport = useCallback((preserveBusyState = false) => {
    abortRef.current?.abort()
    abortRef.current = null
    activeGenerationRef.current = null
    inflightRef.current = false
    resumeSessionRef.current = null
    if (!preserveBusyState) {
      setPendingAssistantId(null)
      setStreaming(false)
    }
  }, [setPendingAssistantId, setStreaming])

  useEffect(() => {
    return () => {
      const rs = resumeSessionRef.current
      if (rs) resumedSessionIds.delete(rs)
      // Unmount/page refresh must not cancel the server-side generation. The
      // persisted request ID is used to recover the draft after remount.
      abortRef.current?.abort()
      abortRef.current = null
      activeGenerationRef.current = null
      inflightRef.current = false
      recoveryControllersRef.current.forEach(controller => controller.abort())
      recoveryControllersRef.current.clear()
      cancellationControllersRef.current.forEach(controller => controller.abort())
      cancellationControllersRef.current.clear()
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

  const patchSessionMessages = useCallback((
    sessionId: string,
    updater: (msgs: ChatPageMessage[]) => ChatPageMessage[],
  ) => {
    patchSessions(base => base.map(s => {
      if (s.id !== sessionId) return s
      const messages = updater(s.messages)
      const title = s.title === '新对话' && messages.some(m => m.role === 'user')
        ? titleFromMessages(messages)
        : s.title
      return { ...s, messages, title, updatedAt: Date.now() }
    }))
  }, [patchSessions])

  const patchActiveMessages = useCallback((updater: (msgs: ChatPageMessage[]) => ChatPageMessage[]) => {
    if (!activeId) return
    patchSessionMessages(activeId, updater)
  }, [activeId, patchSessionMessages])

  const patchSessionMessage = useCallback((
    sessionId: string,
    msgId: string,
    updater: (m: ChatPageMessage) => ChatPageMessage,
  ) => {
    patchSessionMessages(sessionId, msgs => msgs.map(m => (m.id === msgId ? updater(m) : m)))
  }, [patchSessionMessages])

  const patchMessage = useCallback((msgId: string, updater: (m: ChatPageMessage) => ChatPageMessage) => {
    if (!activeId) return
    patchSessionMessage(activeId, msgId, updater)
  }, [activeId, patchSessionMessage])

  const cancelSessionRequests = useCallback((sessionId: string) => {
    const session = sessionsRef.current.find(item => item.id === sessionId)
    if (!session) return
    const requestIds = new Set(
      session.messages
        .map(message => message.generationRequestId)
        .filter((value): value is string => Boolean(value)),
    )
    requestIds.forEach(requestId => {
      cancelledRequestIdsRef.current.add(requestId)
      void cancelGenerationRequest(requestId, sessionId).catch(() => undefined)
    })
  }, [])

  const newSession = useCallback(() => {
    // Switching context detaches the HTTP transport but leaves the server task
    // running. Its request ID remains in the original session for recovery.
    if (streaming) detachTransport()
    const now = Date.now()
    const s: ChatSession = { id: newSessionId(), title: '新对话', messages: [], createdAt: now, updatedAt: now }
    patchSessions(prev => [s, ...prev])
    setActiveId(s.id)
    setError(null)
  }, [streaming, detachTransport, patchSessions, setActiveId, setError])

  const selectSession = useCallback((id: string) => {
    if (streaming) detachTransport()
    setActiveId(id)
    setError(null)
  }, [streaming, detachTransport, setActiveId, setError])

  const deleteSession = useCallback(async (id: string) => {
    if (id === activeId && streaming) detachTransport()
    cancelSessionRequests(id)
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
  }, [activeId, streaming, detachTransport, cancelSessionRequests, patchSessions, setActiveId])

  const clearActive = useCallback(() => {
    if (!activeId) return
    if (streaming) detachTransport()
    cancelSessionRequests(activeId)
    resumedSessionIds.delete(activeId)
    patchActiveMessages(() => [])
  }, [streaming, detachTransport, activeId, cancelSessionRequests, patchActiveMessages])

  const pollDraftPreview = useCallback(async (
    draftId: string,
    msgId: string,
    sessionId: string,
  ) => {
    const result = await pollDraftUntilSettled(draftId, progress => {
      patchSessionMessage(sessionId, msgId, message => {
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
    patchSessionMessage(sessionId, msgId, message => {
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
          awaitingDraftSince: undefined,
        }
      }
      return {
        ...message,
        draft: {
          ...message.draft,
          status: result.kind,
          stage: result.kind,
          errorMessage: result.message,
        },
        awaitingDraft: false,
        awaitingDraftSince: undefined,
      }
    })
    flushToStorage()
  }, [patchSessionMessage, flushToStorage])

  const attachDraft = useCallback((
    sessionId: string,
    assistantId: string,
    draft: ChatDraftState,
  ) => {
    patchSessionMessage(sessionId, assistantId, message => ({
      ...message,
      intent: draft.mediaType === 'image' ? 'generation:image' : 'generation:video',
      model: 'draft',
      draft,
      awaitingDraft: true,
      awaitingDraftSince: Date.now(),
      incomplete: false,
    }))
    flushToStorage()
    void pollDraftPreview(draft.draftId, assistantId, sessionId)
  }, [flushToStorage, patchSessionMessage, pollDraftPreview])

  const recoverGenerationRequest = useCallback(async (
    requestId: string,
    assistantId: string,
    sessionId: string,
  ) => {
    if (recoveryControllersRef.current.has(requestId)) return
    const controller = new AbortController()
    recoveryControllersRef.current.set(requestId, controller)
    try {
      const state = await waitForGenerationRequestDraft(
        requestId,
        sessionId,
        controller.signal,
      )
      if (state.status === 'cancelled') {
        patchSessionMessage(sessionId, assistantId, message => ({
          ...message,
          content: message.content && message.content !== '正在停止…'
            ? message.content
            : '已停止',
          intent: null,
          model: undefined,
          error: false,
          awaitingDraft: false,
          awaitingDraftSince: undefined,
          incomplete: false,
          draft: message.draft ? {
            ...message.draft,
            status: 'cancelled',
            stage: 'cancelled',
            progress: 0,
            errorMessage: '已停止',
          } : message.draft,
        }))
        return
      }
      if (state.draft_id && state.preview_url && state.media_type) {
        attachDraft(sessionId, assistantId, {
          draftId: state.draft_id,
          previewUrl: state.preview_url,
          mediaType: state.media_type,
          status: normalizeDraftStatus(state.status),
          stage: state.stage,
          progress: normalizeDraftProgress(state.progress),
          workflowVersion: state.workflow_version,
          errorMessage: state.error ?? undefined,
        })
      }
    } catch (e) {
      if (controller.signal.aborted) {
        // Module-level recovery markers survive React remounts. Remove the
        // marker so a refreshed component can resume this request again.
        resumedSessionIds.delete(sessionId)
        return
      }
      const code = (e as Error & { code?: string }).code ?? (e instanceof Error ? e.message : '恢复失败')
      const terminal = [
        'generation_request_expired',
        'generation_request_forbidden',
        'unauthorized',
      ].includes(code)
      patchSessionMessage(sessionId, assistantId, message => ({
        ...message,
        content: code === 'generation_request_expired'
          ? '生成请求已过期'
          : `生成请求恢复失败: ${e instanceof Error ? e.message : code}`,
        error: true,
        intent: null,
        model: undefined,
        awaitingDraft: false,
        awaitingDraftSince: undefined,
        incomplete: false,
      }))
      if (!terminal) resumedSessionIds.delete(sessionId)
    } finally {
      recoveryControllersRef.current.delete(requestId)
      flushToStorage()
    }
  }, [attachDraft, flushToStorage, patchSessionMessage])

  const stop = useCallback(() => {
    const activeGeneration = activeGenerationRef.current
    if (!activeGeneration) {
      detachTransport()
      flushToStorage()
      return
    }

    const { requestId, sessionId, assistantId } = activeGeneration
    if (cancellationControllersRef.current.has(requestId)) return
    cancelledRequestIdsRef.current.add(requestId)
    patchSessionMessage(sessionId, assistantId, message => ({
      ...message,
      content: message.content || '正在停止…',
      error: false,
      incomplete: false,
      awaitingDraft: true,
      awaitingDraftSince: message.awaitingDraftSince ?? Date.now(),
      draft: message.draft ? {
        ...message.draft,
        stage: 'cancelling',
        errorMessage: undefined,
      } : message.draft,
    }))
    // Abort only the response transport. Keep the UI busy until the server has
    // persisted the terminal cancellation state.
    detachTransport(true)

    const controller = new AbortController()
    cancellationControllersRef.current.set(requestId, controller)
    void cancelGenerationRequestAndWait(
      requestId,
      sessionId,
      controller.signal,
    ).then(() => {
      patchSessionMessage(sessionId, assistantId, message => ({
        ...message,
        content: message.content && message.content !== '正在停止…'
          ? message.content
          : '已停止',
        intent: null,
        model: undefined,
        error: false,
        incomplete: false,
        awaitingDraft: false,
        awaitingDraftSince: undefined,
        draft: message.draft ? {
          ...message.draft,
          status: 'cancelled',
          stage: 'cancelled',
          progress: 0,
          errorMessage: '已停止',
        } : message.draft,
      }))
    }).catch(error => {
      if (controller.signal.aborted) return
      const message = error instanceof Error ? error.message : '取消失败'
      setError(`停止生成失败: ${message}`)
      patchSessionMessage(sessionId, assistantId, current => ({
        ...current,
        content: '停止失败，正在恢复任务状态',
        error: true,
        incomplete: false,
        awaitingDraft: true,
        awaitingDraftSince: current.awaitingDraftSince ?? Date.now(),
        draft: current.draft ? {
          ...current.draft,
          stage: current.draft.stage === 'cancelling' ? 'running' : current.draft.stage,
          errorMessage: `停止失败: ${message}`,
        } : current.draft,
      }))
      resumedSessionIds.delete(sessionId)
      void recoverGenerationRequest(requestId, assistantId, sessionId)
    }).finally(() => {
      cancellationControllersRef.current.delete(requestId)
      cancelledRequestIdsRef.current.delete(requestId)
      setPendingAssistantId(null)
      setStreaming(false)
      flushToStorage()
    })
  }, [
    detachTransport,
    flushToStorage,
    patchSessionMessage,
    recoverGenerationRequest,
    setError,
    setPendingAssistantId,
    setStreaming,
  ])

  const send = useCallback(async (
    text: string,
    opts?: {
      resume?: boolean
      dropLastAssistant?: boolean
      generationOptions?: SourceAwareGenerationOptions
      referenceImage?: ChatReferenceImage
    },
  ) => {
    const trimmed = text.trim()
    if (!trimmed || streaming || inflightRef.current || !activeId) return
    inflightRef.current = true
    const sessionId = activeId
    const isResume = Boolean(opts?.resume)
    const requestId = newGenerationRequestId()
    setError(null)
    if (!isResume) resumedSessionIds.delete(sessionId)

    const userMsg: ChatPageMessage = {
      id: nextMessageId(),
      role: 'user',
      content: trimmed,
      referenceImageDataUrl: opts?.referenceImage?.dataUrl,
      referenceImageName: opts?.referenceImage?.name,
      ts: Date.now(),
    }
    const assistantId = nextMessageId()
    const sourceDraftId = opts?.generationOptions?.source_draft_id
    const assistantMsg: ChatPageMessage = {
      id: assistantId,
      role: 'assistant',
      content: '',
      intent: sourceDraftId ? 'generation:video' : undefined,
      model: sourceDraftId ? 'source-draft' : undefined,
      generationRequestId: requestId,
      awaitingDraft: true,
      awaitingDraftSince: Date.now(),
      ts: Date.now(),
    }

    const cur = sessionsRef.current.find(x => x.id === sessionId)
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

    patchSessionMessages(sessionId, msgs => isResume ? [...msgs, assistantMsg] : [...msgs, userMsg, assistantMsg])
    setPendingAssistantId(assistantId)
    flushToStorage()

    setStreaming(true)
    const controller = new AbortController()
    abortRef.current = controller
    activeGenerationRef.current = { requestId, sessionId, assistantId, controller }
    if (isResume) resumeSessionRef.current = sessionId

    try {
      if (sourceDraftId) {
        const created = await createVideoDraftFromSource(
          sourceDraftId,
          {
            requestId,
            motionPrompt: trimmed,
            durationSeconds: opts?.generationOptions?.duration_seconds ?? 5,
            fps: opts?.generationOptions?.fps ?? 8,
            chatSessionId: sessionId,
          },
          controller.signal,
        )
        attachDraft(sessionId, assistantId, {
          draftId: created.draft_id,
          previewUrl: created.preview_url,
          mediaType: 'video',
          status: normalizeDraftStatus(created.status),
          stage: 'preview_ready',
          progress: 1,
          progressSource: 'complete',
        })
        setPendingAssistantId(null)
        setStreaming(false)
        return
      }

      const generationOptions = opts?.generationOptions
        ? Object.fromEntries(
            Object.entries(opts.generationOptions).filter(([key]) => key !== 'source_draft_id'),
          ) as GenerationOptions
        : undefined
      const resp = await requestChatCompletion(
        {
          model: 'auto',
          messages: wireMessages,
          stream: true,
          chat_session_id: sessionId,
          generation_options: generationOptions,
        },
        controller.signal,
        requestId,
      )

      if (resp.kind === 'draft') {
        attachDraft(sessionId, assistantId, {
          draftId: resp.draftId,
          previewUrl: resp.previewUrl,
          mediaType: resp.mediaType,
          status: 'generating',
        })
        setPendingAssistantId(null)
        setStreaming(false)
        return
      }

      patchSessionMessage(sessionId, assistantId, message => ({
        ...message,
        awaitingDraft: false,
        awaitingDraftSince: undefined,
      }))
      await consumeChatEventStream(resp.body, chunk => {
        const delta = chunk.choices?.[0]?.delta
        const meta = chunk._meta?.routed_to
        const streamError = chunk.error
        const errorMessage = streamError?.message ?? streamError?.code ?? '请求失败'
        patchSessionMessage(sessionId, assistantId, message => {
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
      const explicitlyCancelled = controller.signal.aborted
        && cancelledRequestIdsRef.current.has(requestId)
      if (controller.signal.aborted) {
        if (explicitlyCancelled) {
          patchSessionMessage(sessionId, assistantId, message => ({
            ...message,
            content: message.content || '正在停止…',
            error: false,
            incomplete: false,
            awaitingDraft: true,
            awaitingDraftSince: message.awaitingDraftSince ?? Date.now(),
          }))
        } else {
          // Transport detached during navigation/refresh. Preserve the request
          // identity so the session can resolve the server-created draft.
          patchSessionMessage(sessionId, assistantId, message => ({
            ...message,
            awaitingDraft: true,
            awaitingDraftSince: message.awaitingDraftSince ?? Date.now(),
          }))
        }
      } else {
        const code = (e as Error & { code?: string }).code
        const msg = e instanceof Error ? e.message : '请求失败'
        setError(msg)
        patchSessionMessage(sessionId, assistantId, message => ({
          ...message,
          content: msg,
          error: true,
          intent: null,
          model: undefined,
          awaitingDraft: false,
          awaitingDraftSince: undefined,
          incomplete: false,
          generationRequestId: code === 'reference_image_required'
            ? undefined
            : message.generationRequestId,
        }))
      }
      if (!explicitlyCancelled) {
        setStreaming(false)
        setPendingAssistantId(null)
      }
      flushToStorage()
    } finally {
      if (abortRef.current === controller) abortRef.current = null
      if (activeGenerationRef.current?.requestId === requestId) {
        activeGenerationRef.current = null
      }
      inflightRef.current = false
      if (isResume) resumeSessionRef.current = null
    }
  }, [
    activeId,
    attachDraft,
    flushToStorage,
    patchSessionMessage,
    patchSessionMessages,
    setError,
    setPendingAssistantId,
    setStreaming,
    streaming,
  ])

  const sendRef = useRef(send)
  useEffect(() => { sendRef.current = send }, [send])

  useEffect(() => {
    if (!activeId || resumedSessionIds.has(activeId)) return
    const s = sessionsRef.current.find(x => x.id === activeId)
    if (!s || s.messages.length === 0) return
    resumedSessionIds.add(activeId)

    for (const m of s.messages) {
      if (m.role !== 'assistant') continue
      if (m.generationRequestId && m.awaitingDraft && !m.draft) {
        void recoverGenerationRequest(m.generationRequestId, m.id, s.id)
        continue
      }
      if (!m.draft) continue
      const st = m.draft.status
      if (st === 'generating' || m.awaitingDraft || st === 'pending' || st === 'confirming' || st === 'rejecting') {
        if (st !== 'generating') {
          patchSessionMessage(s.id, m.id, message => message.draft
            ? { ...message, draft: { ...message.draft, status: 'pending', errorMessage: undefined } }
            : message)
        }
        if (!m.draft.previewDataUrl) void pollDraftPreview(m.draft.draftId, m.id, s.id)
      } else if (st === 'confirmed') {
        if (!m.draft.resultDataUrl) {
          void getDraftResult(m.draft.draftId).then(
            ({ resultDataUrl }) => patchSessionMessage(s.id, m.id, message => message.draft
              ? { ...message, draft: { ...message.draft, resultDataUrl } }
              : message),
            (e: unknown) => patchSessionMessage(s.id, m.id, message => message.draft
              ? { ...message, draft: { ...message.draft, resultLost: true, errorMessage: e instanceof Error ? e.message : '结果加载失败' } }
              : message),
          )
        }
        if (!m.draft.previewDataUrl) void pollDraftPreview(m.draft.draftId, m.id, s.id)
      }
    }

    if (s.messages.some(hasActiveAsyncTask)) return
    const last = s.messages[s.messages.length - 1]
    let needResumeSend = false
    let resumeText: string | null = null
    let dropLastAssistant = false
    if (last.role === 'user') {
      patchSessionMessages(s.id, msgs => msgs.filter(m => !(m.role === 'assistant' && !m.content && !m.draft)))
      needResumeSend = true
      resumeText = last.content
    } else if (last.role === 'assistant' && (last.incomplete || (!last.content && !last.draft))) {
      if (last.generationRequestId || last.awaitingDraft || last.draft?.status === 'generating') return
      if (!last.content && !last.draft && pendingAssistantIdRef.current === last.id) return
      patchSessionMessages(s.id, msgs => msgs.slice(0, -1))
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
      if (activeId) void pollDraftPreview(newDraftId, msgId, activeId)
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
