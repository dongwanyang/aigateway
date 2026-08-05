import { create } from 'zustand'
import type { ChatPageMessage, ChatSession } from '@/types'
import { loadActiveId, loadSessions } from '@/services/chatStorage'

type Updater<T> = T | ((previous: T) => T)

function resolve<T>(next: Updater<T>, previous: T): T {
  return typeof next === 'function'
    ? (next as (value: T) => T)(previous)
    : next
}

function preserveCancelledMessage(
  previous: ChatPageMessage | undefined,
  next: ChatPageMessage,
): ChatPageMessage {
  const previousDraft = previous?.draft
  const nextDraft = next.draft
  if (
    previousDraft?.status !== 'cancelled'
    || !nextDraft
    || nextDraft.draftId !== previousDraft.draftId
    || nextDraft.status === 'cancelled'
  ) return next

  return {
    ...next,
    intent: null,
    model: undefined,
    error: false,
    incomplete: false,
    awaitingDraft: false,
    awaitingDraftSince: undefined,
    draft: previousDraft,
  }
}

function preserveCancelledDrafts(
  previousSessions: ChatSession[],
  nextSessions: ChatSession[],
): ChatSession[] {
  const previousById = new Map(previousSessions.map(session => [session.id, session]))
  return nextSessions.map(session => {
    const previous = previousById.get(session.id)
    if (!previous) return session
    const previousMessages = new Map(previous.messages.map(message => [message.id, message]))
    return {
      ...session,
      messages: session.messages.map(message => preserveCancelledMessage(
        previousMessages.get(message.id),
        message,
      )),
    }
  })
}

interface ChatStore {
  sessions: ChatSession[]
  activeId: string | null
  streaming: boolean
  error: string | null
  pendingAssistantId: string | null
  resumePollingKey: number
  setSessions: (next: Updater<ChatSession[]>) => void
  setActiveId: (next: Updater<string | null>) => void
  setStreaming: (next: Updater<boolean>) => void
  setError: (next: Updater<string | null>) => void
  setPendingAssistantId: (next: Updater<string | null>) => void
  setResumePollingKey: (next: Updater<number>) => void
}

const initialSessions = loadSessions()

export const useChatStore = create<ChatStore>((set) => ({
  sessions: initialSessions,
  activeId: loadActiveId(initialSessions),
  streaming: false,
  error: null,
  pendingAssistantId: null,
  resumePollingKey: 0,
  setSessions: next => set(state => ({
    sessions: preserveCancelledDrafts(
      state.sessions,
      resolve(next, state.sessions),
    ),
  })),
  setActiveId: next => set(state => ({ activeId: resolve(next, state.activeId) })),
  setStreaming: next => set(state => ({ streaming: resolve(next, state.streaming) })),
  setError: next => set(state => ({ error: resolve(next, state.error) })),
  setPendingAssistantId: next => set(state => ({
    pendingAssistantId: resolve(next, state.pendingAssistantId),
  })),
  setResumePollingKey: next => set(state => ({
    resumePollingKey: resolve(next, state.resumePollingKey),
  })),
}))
