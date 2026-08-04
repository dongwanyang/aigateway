import type { ChatDraftState, ChatPageMessage, ChatSession } from '@/types'

export const CHAT_SESSIONS_KEY = 'aigateway:chat:sessions'
export const CHAT_ACTIVE_KEY = 'aigateway:chat:active'
const LEGACY_MESSAGES_KEY = 'aigateway:chat:messages'

export function titleFromMessages(messages: ChatPageMessage[]): string {
  const firstUser = messages.find(message => message.role === 'user')
  if (!firstUser) return '新对话'
  return firstUser.content.trim().slice(0, 20) || '新对话'
}

export function loadSessions(): ChatSession[] {
  if (typeof window === 'undefined') return []
  try {
    const raw = localStorage.getItem(CHAT_SESSIONS_KEY)
    if (raw) {
      const parsed: unknown = JSON.parse(raw)
      if (Array.isArray(parsed)) return parsed as ChatSession[]
    }
  } catch {
    // Fall through to legacy migration.
  }

  try {
    const legacyRaw = localStorage.getItem(LEGACY_MESSAGES_KEY)
    if (!legacyRaw) return []
    const legacy: unknown = JSON.parse(legacyRaw)
    if (!Array.isArray(legacy) || legacy.length === 0) return []
    const now = Date.now()
    const messages = legacy as ChatPageMessage[]
    const migrated: ChatSession = {
      id: 'migrated',
      title: titleFromMessages(messages),
      messages,
      createdAt: now,
      updatedAt: now,
    }
    localStorage.setItem(CHAT_SESSIONS_KEY, JSON.stringify([migrated]))
    localStorage.setItem(CHAT_ACTIVE_KEY, migrated.id)
    localStorage.removeItem(LEGACY_MESSAGES_KEY)
    return [migrated]
  } catch {
    return []
  }
}

export function loadActiveId(sessions: ChatSession[]): string | null {
  if (typeof window === 'undefined') return sessions[0]?.id ?? null
  try {
    const id = localStorage.getItem(CHAT_ACTIVE_KEY)
    if (id && sessions.some(session => session.id === id)) return id
  } catch {
    // Use the first available session.
  }
  return sessions[0]?.id ?? null
}

export function serializeSessions(sessions: ChatSession[]): string {
  return JSON.stringify(sessions.map(session => ({
    ...session,
    messages: session.messages.map(message => {
      const {
        referenceImageDataUrl: _referenceImage,
        ...messageWithoutReferenceData
      } = message
      if (!message.draft) return messageWithoutReferenceData
      const { previewDataUrl: _preview, resultDataUrl: _result, ...draft } = message.draft
      return { ...messageWithoutReferenceData, draft: draft as ChatDraftState }
    }),
  })))
}

export function persistSessions(sessions: ChatSession[]): void {
  localStorage.setItem(CHAT_SESSIONS_KEY, serializeSessions(sessions))
}

export function persistActiveId(activeId: string): void {
  localStorage.setItem(CHAT_ACTIVE_KEY, activeId)
}
