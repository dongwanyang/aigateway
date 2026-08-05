import { cancelGenerationRequestAndWait } from '@/api/generationRequest'
import { persistSessions } from '@/services/chatStorage'
import { useChatStore } from '@/stores/chatStore'
import type { ChatPageMessage, ChatSession } from '@/types'

const CANCELLABLE_DRAFT_STATUSES = new Set([
  'queued',
  'running',
  'generating',
  'confirming',
  'refining',
])
const cancellingRequestIds = new Set<string>()

function isCancellable(message: ChatPageMessage): boolean {
  if (message.role !== 'assistant' || !message.generationRequestId) return false
  return Boolean(
    message.awaitingDraft
    || (message.draft && CANCELLABLE_DRAFT_STATUSES.has(message.draft.status)),
  )
}

function cancellableMessages(session: ChatSession): ChatPageMessage[] {
  return session.messages.filter(isCancellable)
}

function latestCancellableMessage(session: ChatSession): ChatPageMessage | undefined {
  return [...cancellableMessages(session)].reverse()[0]
}

function patchSessionMessage(
  sessionId: string,
  messageId: string,
  updater: (message: ChatPageMessage) => ChatPageMessage,
): void {
  const store = useChatStore.getState()
  const next = store.sessions.map(session => session.id === sessionId
    ? {
        ...session,
        messages: session.messages.map(message => message.id === messageId
          ? updater(message)
          : message),
        updatedAt: Date.now(),
      }
    : session)
  store.setSessions(next)
  try { persistSessions(next) } catch { /* UI state remains authoritative */ }
}

async function cancelMessageGeneration(
  sessionId: string,
  message: ChatPageMessage,
): Promise<boolean> {
  const requestId = message.generationRequestId
  if (!requestId) return true
  if (cancellingRequestIds.has(requestId)) return false

  cancellingRequestIds.add(requestId)
  useChatStore.getState().setError(null)
  patchSessionMessage(sessionId, message.id, current => ({
    ...current,
    content: current.content || '正在停止…',
    error: false,
    incomplete: false,
    awaitingDraft: true,
    awaitingDraftSince: current.awaitingDraftSince ?? Date.now(),
    draft: current.draft ? {
      ...current.draft,
      stage: 'cancelling',
      errorMessage: undefined,
    } : current.draft,
  }))

  try {
    await cancelGenerationRequestAndWait(requestId, sessionId)
    patchSessionMessage(sessionId, message.id, current => ({
      ...current,
      content: current.content && current.content !== '正在停止…'
        ? current.content
        : '已停止',
      intent: null,
      model: undefined,
      error: false,
      incomplete: false,
      awaitingDraft: false,
      awaitingDraftSince: undefined,
      draft: current.draft ? {
        ...current.draft,
        status: 'cancelled',
        stage: 'cancelled',
        progress: 0,
        errorMessage: '已停止',
      } : current.draft,
    }))
    return true
  } catch (error) {
    const reason = error instanceof Error ? error.message : '取消失败'
    useChatStore.getState().setError(`停止生成失败: ${reason}`)
    patchSessionMessage(sessionId, message.id, current => ({
      ...current,
      content: current.content === '正在停止…' ? '停止失败，任务仍在运行' : current.content,
      error: true,
      incomplete: false,
      awaitingDraft: Boolean(current.awaitingDraft || current.draft),
      awaitingDraftSince: current.awaitingDraftSince ?? Date.now(),
      draft: current.draft ? {
        ...current.draft,
        stage: current.draft.stage === 'cancelling' ? 'running' : current.draft.stage,
        errorMessage: `停止失败: ${reason}`,
      } : current.draft,
    }))
    return false
  } finally {
    cancellingRequestIds.delete(requestId)
    const latestStore = useChatStore.getState()
    latestStore.setPendingAssistantId(null)
    latestStore.setStreaming(false)
  }
}

/**
 * Cancel the latest active generation in a session after the original HTTP
 * request has already returned. The message reaches `cancelled` only after the
 * server confirms the persisted terminal state.
 */
export async function cancelLatestSessionGeneration(sessionId: string): Promise<boolean> {
  const session = useChatStore.getState().sessions.find(item => item.id === sessionId)
  const message = session ? latestCancellableMessage(session) : undefined
  return message ? cancelMessageGeneration(sessionId, message) : false
}

/**
 * Destructive session cleanup must not race active backend/ComfyUI work. Every
 * active request is cancelled and confirmed before the caller deletes draft
 * files or removes the local session. A single failure blocks cleanup.
 */
export async function cancelAllSessionGenerations(sessionId: string): Promise<boolean> {
  const session = useChatStore.getState().sessions.find(item => item.id === sessionId)
  if (!session) return true
  const messages = cancellableMessages(session)
  for (const message of messages) {
    if (!await cancelMessageGeneration(sessionId, message)) return false
  }
  return true
}
