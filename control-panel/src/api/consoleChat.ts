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

export async function requestChatCompletion(
  body: ChatCompletionRequest & { chat_session_id?: string },
  signal?: AbortSignal,
): Promise<ChatResponse> {
  const headers = await ensureAuthHeaders()
  const res = await fetch(`${API_BASE}/admin/console/chat/completions`, {
    method: 'POST',
    credentials: 'include',
    headers: { ...headers, 'Accept': 'text/event-stream' },
    body: JSON.stringify({ ...body, stream: true }),
    signal,
  })

  if (!res.ok) {
    let errorMsg = `HTTP ${res.status}`
    try {
      const errBody = (await res.json()) as ApiError
      errorMsg = errBody.error?.message || errorMsg
    } catch {
      // Non-JSON error response (e.g. HTML nginx page); use status code
    }
    throw new Error(errorMsg)
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
      throw new Error('草稿响应缺少 draft_id / preview_url')
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
    throw new Error('Streaming response has no body')
  }
  return { kind: 'stream', body: res.body }
}
