import { getGenerationRequest } from './generationRequest'
import type { ApiError, ChatCompletionRequest } from '@/types'

const API_BASE = import.meta.env.VITE_API_BASE ?? ''
const RECOVERY_ATTEMPTS = 120
const RECOVERY_INTERVAL_MS = 250

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

function sleep(ms: number): Promise<void> {
  return new Promise(resolve => window.setTimeout(resolve, ms))
}

async function recoverDraftAfterTransportFailure(
  requestId: string,
  chatSessionId: string,
): Promise<ChatResponse | null> {
  for (let attempt = 0; attempt < RECOVERY_ATTEMPTS; attempt += 1) {
    try {
      const state = await getGenerationRequest(requestId, chatSessionId)
      if (state.draft_id && state.preview_url && state.media_type) {
        return {
          kind: 'draft',
          draftId: state.draft_id,
          previewUrl: state.preview_url,
          mediaType: state.media_type,
          generationParams: {
            request_id: state.request_id,
            workflow_version: state.workflow_version,
          },
        }
      }
      if (state.status === 'cancelled') {
        throw new ChatRequestError('生成请求已取消', 'generation_cancelled', 409)
      }
      await sleep(state.retry_after_ms ?? RECOVERY_INTERVAL_MS)
    } catch (error) {
      if (error instanceof ChatRequestError) throw error
      const status = (error as Error & { status?: number }).status
      const code = (error as Error & { code?: string }).code
      if (status === 403 || status === 410 || code === 'generation_request_expired') {
        return null
      }
      await sleep(RECOVERY_INTERVAL_MS)
    }
  }
  return null
}

export async function requestChatCompletion(
  body: ChatCompletionRequest & { chat_session_id?: string },
  signal?: AbortSignal,
  requestId?: string,
): Promise<ChatResponse> {
  const headers = await ensureAuthHeaders()
  const messages = normalizeChatMessages(body.messages)
  const requestHeaders: Record<string, string> = {
    ...headers,
    'Accept': 'text/event-stream',
  }
  if (requestId) requestHeaders['X-Request-ID'] = requestId

  let res: Response
  try {
    res = await fetch(`${API_BASE}/admin/console/chat/completions`, {
      method: 'POST',
      credentials: 'include',
      headers: requestHeaders,
      body: JSON.stringify({ ...body, messages, stream: true }),
      signal,
    })
  } catch (error) {
    if (signal?.aborted) throw error
    if (requestId && body.chat_session_id) {
      const recovered = await recoverDraftAfterTransportFailure(
        requestId,
        body.chat_session_id,
      )
      if (recovered) return recovered
    }
    throw error
  }

  if (!res.ok) {
    let details = { code: 'unknown_error', message: `HTTP ${res.status}` }
    try {
      details = errorDetails(await res.json(), details.message)
    } catch {
      // Non-JSON error response; retain status text.
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
