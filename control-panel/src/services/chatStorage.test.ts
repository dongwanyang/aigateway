import { beforeEach, describe, expect, it } from 'vitest'
import type { ChatSession } from '@/types'
import {
  CHAT_ACTIVE_KEY,
  CHAT_SESSIONS_KEY,
  loadActiveId,
  loadSessions,
  persistSessions,
} from './chatStorage'

const session: ChatSession = {
  id: 'session-1',
  title: '测试会话',
  createdAt: 1,
  updatedAt: 2,
  messages: [{
    id: 'message-1',
    role: 'assistant',
    content: '',
    ts: 2,
    draft: {
      draftId: 'draft-1',
      previewUrl: '/preview',
      mediaType: 'image',
      status: 'confirmed',
      previewDataUrl: 'data:image/png;base64,preview',
      resultDataUrl: 'data:image/png;base64,result',
    },
  }],
}

describe('chatStorage', () => {
  beforeEach(() => localStorage.clear())

  it('persists sessions without large draft data URLs', () => {
    persistSessions([session])

    const stored = JSON.parse(localStorage.getItem(CHAT_SESSIONS_KEY) ?? '[]')
    expect(stored[0].messages[0].draft.previewDataUrl).toBeUndefined()
    expect(stored[0].messages[0].draft.resultDataUrl).toBeUndefined()
    expect(stored[0].messages[0].draft.draftId).toBe('draft-1')
  })

  it('does not persist uploaded reference image data URLs', () => {
    const withReference: ChatSession = {
      ...session,
      messages: [{
        id: 'user-reference',
        role: 'user',
        content: '生成视频',
        referenceImageName: 'reference.png',
        referenceImageDataUrl: 'data:image/png;base64,large-payload',
        ts: 1,
      }],
    }

    persistSessions([withReference])

    const stored = JSON.parse(localStorage.getItem(CHAT_SESSIONS_KEY) ?? '[]')
    expect(stored[0].messages[0].referenceImageDataUrl).toBeUndefined()
    expect(stored[0].messages[0].referenceImageName).toBe('reference.png')
  })

  it('migrates the legacy single-session message list', () => {
    localStorage.setItem('aigateway:chat:messages', JSON.stringify([{
      id: 'legacy-message',
      role: 'user',
      content: '迁移后的标题',
      ts: 1,
    }]))

    const migrated = loadSessions()

    expect(migrated).toHaveLength(1)
    expect(migrated[0].title).toBe('迁移后的标题')
    expect(localStorage.getItem(CHAT_ACTIVE_KEY)).toBe('migrated')
    expect(localStorage.getItem('aigateway:chat:messages')).toBeNull()
  })

  it('falls back to the first session when the active id is stale', () => {
    localStorage.setItem(CHAT_ACTIVE_KEY, 'missing')
    expect(loadActiveId([session])).toBe('session-1')
  })
})
