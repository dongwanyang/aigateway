import { create } from 'zustand'
import type { ChatSession } from '@/types'
import { loadActiveId, loadSessions } from '@/services/chatStorage'

type Updater<T> = T | ((previous: T) => T)

function resolve<T>(next: Updater<T>, previous: T): T {
  return typeof next === 'function'
    ? (next as (value: T) => T)(previous)
    : next
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
  setSessions: next => set(state => ({ sessions: resolve(next, state.sessions) })),
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
