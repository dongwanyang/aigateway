import { getDraftPreview, getVideoStatus } from '@/api/client'
import type { ChatPageMessage, VideoStatusResponse } from '@/types'

let messageIdCounter = 0

export const resumedSessionIds = new Set<string>()
export const pollingVideoIds = new Set<string>()
export const pollingDraftIds = new Set<string>()

export const VIDEO_POLL_INTERVAL_MS = 5_000
export const VIDEO_POLL_MAX_ATTEMPTS = 360
export const DRAFT_POLL_INTERVAL_MS = 1_000
export const DRAFT_POLL_MAX_ATTEMPTS = 120

export function nextMessageId(): string {
  messageIdCounter += 1
  return `msg-${Date.now()}-${messageIdCounter}`
}

export function newSessionId(): string {
  return `sess-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
}

export function clearAllChatPolling(): void {
  pollingVideoIds.clear()
  pollingDraftIds.clear()
}

export function wait(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms))
}

export type DraftPollResult =
  | { kind: 'duplicate' }
  | { kind: 'ready'; previewDataUrl: string }
  | { kind: 'expired'; message: string }
  | { kind: 'error'; message: string }

export async function pollDraftUntilSettled(draftId: string): Promise<DraftPollResult> {
  if (pollingDraftIds.has(draftId)) return { kind: 'duplicate' }
  pollingDraftIds.add(draftId)
  try {
    for (let attempt = 0; attempt < DRAFT_POLL_MAX_ATTEMPTS; attempt += 1) {
      await wait(DRAFT_POLL_INTERVAL_MS)
      try {
        const response = await getDraftPreview(draftId)
        if (response.status === 'generating') continue
        return response.previewDataUrl
          ? { kind: 'ready', previewDataUrl: response.previewDataUrl }
          : { kind: 'error', message: '预览响应缺少 data URL' }
      } catch (error) {
        const message = error instanceof Error ? error.message : '预览加载失败'
        if (message.includes('not_found') || message.includes('expired')) {
          return { kind: 'expired', message }
        }
        if (message.includes('draft_failed')) {
          return { kind: 'error', message }
        }
        console.warn(`Failed to poll draft preview for ${draftId}:`, error)
      }
    }
    return { kind: 'expired', message: '草稿生成超时' }
  } finally {
    pollingDraftIds.delete(draftId)
  }
}

function isVideoTerminal(status: string | undefined): boolean {
  return ['succeeded', 'completed', 'failed', 'error', 'expired'].includes(status ?? '')
}

export async function pollVideoUntilTerminal(videoId: string): Promise<VideoStatusResponse | null> {
  if (pollingVideoIds.has(videoId)) return null
  pollingVideoIds.add(videoId)
  try {
    for (let attempt = 0; attempt < VIDEO_POLL_MAX_ATTEMPTS; attempt += 1) {
      await wait(VIDEO_POLL_INTERVAL_MS)
      try {
        const status = await getVideoStatus(videoId)
        if (isVideoTerminal(status.status)) return status
      } catch (error) {
        console.warn(`Failed to poll video status for ${videoId}:`, error)
      }
    }
    return null
  } finally {
    pollingVideoIds.delete(videoId)
  }
}

export interface ChatStreamChunk {
  choices?: Array<{ delta?: { content?: string } }>
  _meta?: {
    routed_to?: { intent?: ChatPageMessage['intent']; model?: string }
    video_id?: string
  }
  error?: { message?: string; code?: string }
}

export async function consumeChatEventStream(
  stream: ReadableStream<Uint8Array>,
  onChunk: (chunk: ChatStreamChunk) => void,
): Promise<void> {
  const reader = stream.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) return
      buffer += decoder.decode(value, { stream: true })
      let boundary = buffer.indexOf('\n\n')
      while (boundary !== -1) {
        const frame = buffer.slice(0, boundary).trim()
        buffer = buffer.slice(boundary + 2)
        boundary = buffer.indexOf('\n\n')
        if (!frame.startsWith('data:')) continue
        const payload = frame.slice(5).trim()
        if (payload === '[DONE]') return
        try {
          onChunk(JSON.parse(payload) as ChatStreamChunk)
        } catch {
          // Ignore malformed/non-JSON frames without terminating the stream.
        }
      }
    }
  } finally {
    try {
      reader.releaseLock()
    } catch {
      // The stream may already have released its reader after an error.
    }
  }
}
