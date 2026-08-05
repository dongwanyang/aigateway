const API_BASE = import.meta.env.VITE_API_BASE ?? ''

export interface GenerationRequestState {
  request_id: string
  draft_id?: string
  status: string
  stage?: string
  progress?: number
  media_type?: 'image' | 'video'
  preview_url?: string
  expires_at?: number
  workflow_version?: string
  error?: string | null
  retry_after_ms?: number
}

export function newGenerationRequestId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return `req-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 14)}`
}

function requestPath(requestId: string, chatSessionId: string): string {
  const query = new URLSearchParams({ chat_session_id: chatSessionId })
  return `${API_BASE}/admin/generation/requests/${encodeURIComponent(requestId)}?${query}`
}

async function parseError(response: Response, fallback: string): Promise<Error> {
  const body = await response.json().catch(() => null) as {
    error?: { code?: string; message?: string }
    detail?: { error?: { code?: string; message?: string } } | string
  } | null
  const nested = typeof body?.detail === 'object' ? body.detail.error : undefined
  const code = body?.error?.code ?? nested?.code ?? 'unknown_error'
  const message = body?.error?.message
    ?? nested?.message
    ?? (typeof body?.detail === 'string' ? body.detail : fallback)
  const error = new Error(message)
  ;(error as Error & { code?: string; status?: number }).code = code
  ;(error as Error & { code?: string; status?: number }).status = response.status
  return error
}

export async function getGenerationRequest(
  requestId: string,
  chatSessionId: string,
  signal?: AbortSignal,
): Promise<GenerationRequestState> {
  const response = await fetch(requestPath(requestId, chatSessionId), {
    credentials: 'include',
    signal,
  })
  if (response.status === 202) {
    return response.json() as Promise<GenerationRequestState>
  }
  if (!response.ok) {
    throw await parseError(response, `查询生成请求失败: HTTP ${response.status}`)
  }
  return response.json() as Promise<GenerationRequestState>
}

export async function cancelGenerationRequest(
  requestId: string,
  chatSessionId: string,
): Promise<GenerationRequestState> {
  const response = await fetch(requestPath(requestId, chatSessionId), {
    method: 'DELETE',
    credentials: 'include',
  })
  if (response.status === 202) {
    return response.json() as Promise<GenerationRequestState>
  }
  if (!response.ok) {
    throw await parseError(response, `取消生成请求失败: HTTP ${response.status}`)
  }
  return response.json() as Promise<GenerationRequestState>
}
