import type { ApiError, ChatCompletionRequest } from '@/types'

const API_BASE = import.meta.env.VITE_API_BASE ?? ''

async function ensureAuthHeaders(): Promise<Record<string, string>> {
  return { 'Content-Type': 'application/json' }
}

export type ChatResponse =
  | { kind: 'stream'; body: ReadableStream<Uint8Array> }
  | {
      kind: 'draft'
      draftId: string
      previewUrl: string
      mediaType: 'image' | 'video'
      generationParams: Record<string, unknown>
    }

export class ChatRequestError extends Error {
  readonly code: string
  readonly status: number

  constructor(message: string, code: string, status: number) {
    super(message)
    this.name = 'ChatRequestError'
    this.code = code
    this.status = status
  }
}

function contentFingerprint(content: unknown): string | null {
  try {
    return JSON.stringify(content)
  } catch {
    return null
  }
}

/**
 * Remove only duplicate terminal user turns.
 *
 * Older useChatSessions code persisted the current user turn before constructing
 * the wire payload, then appended the same object again. This duplicated text
 * and image_url blocks. Restricting normalization to adjacent terminal user
 * turns preserves legitimate repeated prompts separated by an assistant reply.
 */
export function normalizeChatMessages(
  messages: ChatCompletionRequest['messages'],
): ChatCompletionRequest['messages'] {
  const normalized = [...messages]
  while (normalized.length >= 2) {
    const previous = normalized[normalized.length - 2]
    const current = normalized[normalized.length - 1]
    if (previous.role !== 'user' || current.role !== 'user') break
    const previousFingerprint = contentFingerprint(previous.content)
    const currentFingerprint = contentFingerprint(current.content)
    if (
      previousFingerprint === null
      || currentFingerprint === null
      || previousFingerprint !== currentFingerprint
    ) break
    normalized.pop()
  }
  return normalized
}

function errorDetails(body: unknown, fallback: string): { code: string; message: string } {
  if (!body || typeof body !== 'object') {
    return { code: 'unknown_error', message: fallback }
  }
  const value = body as ApiError & {
    detail?: { error?: { code?: string; message?: string } } | string
  }
  const nested = typeof value.detail === 'object' ? value.detail?.error : undefined
  return {
    code: value.error?.code ?? nested?.code ?? 'unknown_error',
    message: value.error?.message
      ?? nested?.message
      ?? (typeof value.detail === 'string' ? value.detail : fallback),
  }
}

export async function requestChatCompletion(
  body: ChatCompletionRequest & { chat_session_id?: string },
  signal?: AbortSignal,
): Promise<ChatResponse> {
  const headers = await ensureAuthHeaders()
  const messages = normalizeChatMessages(body.messages)
  const res = await fetch(`${API_BASE}/admin/console/chat/completions`, {
    method: 'POST',
    credentials: 'include',
    headers: { ...headers, 'Accept': 'text/event-stream' },
    body: JSON.stringify({ ...body, messages, stream: true }),
    signal,
  })

  if (!res.ok) {
    let details = { code: 'unknown_error', message: `HTTP ${res.status}` }
    try {
      details = errorDetails(await res.json(), details.message)
    } catch {
      // Non-JSON error response (for example an nginx page); retain status text.
    }
    throw new ChatRequestError(details.message, details.code, res.status)
  }

  const contentType = res.headers.get('content-type') ?? ''

  if (contentType.includes('application/json')) {
    const json = (await res.json()) as {
      data?: { draft_id?: string; preview_url?: string; generation_params?: { media_type?: string } & Record<string, unknown> }
      _meta?: { draft_pending_confirmation?: boolean }
    }
    const draftId = json.data?.draft_id
    const previewUrl = json.data?.preview_url
    if (!draftId || !previewUrl) {
      throw new ChatRequestError(
        '草稿响应缺少 draft_id / preview_url',
        'invalid_draft_response',
        502,
      )
    }
    const mediaTypeRaw = json.data?.generation_params?.media_type
    const mediaType: 'image' | 'video' = mediaTypeRaw === 'video' ? 'video' : 'image'
    return {
      kind: 'draft',
      draftId,
      previewUrl,
      mediaType,
      generationParams: json.data?.generation_params ?? {},
    }
  }

  if (!res.body) {
    throw new ChatRequestError(
      'Streaming response has no body',
      'invalid_stream_response',
      502,
    )
  }
  return { kind: 'stream', body: res.body }
}
