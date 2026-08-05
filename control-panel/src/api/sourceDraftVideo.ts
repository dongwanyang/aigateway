const API_BASE = import.meta.env.VITE_API_BASE ?? ''

export interface CreateSourceDraftVideoRequest {
  motionPrompt: string
  durationSeconds: 3 | 5 | 8
  fps: number
  chatSessionId: string
}

export interface SourceDraftVideoResponse {
  source_draft_id: string
  draft_id: string
  status: string
  media_type: 'video'
  preview_url: string
  source_image_sha256: string
  duration_seconds: number
  fps: number
  frame_count: number
  expires_at: number
}

export class SourceDraftVideoError extends Error {
  readonly code: string
  readonly status: number

  constructor(message: string, code: string, status: number) {
    super(message)
    this.name = 'SourceDraftVideoError'
    this.code = code
    this.status = status
  }
}

function errorDetails(body: unknown, fallback: string): { code: string; message: string } {
  if (!body || typeof body !== 'object') {
    return { code: 'unknown_error', message: fallback }
  }
  const value = body as {
    error?: { code?: string; message?: string }
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

export async function createVideoDraftFromSource(
  sourceDraftId: string,
  request: CreateSourceDraftVideoRequest,
  signal?: AbortSignal,
): Promise<SourceDraftVideoResponse> {
  const response = await fetch(
    `${API_BASE}/admin/draft/${encodeURIComponent(sourceDraftId)}/video`,
    {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        motion_prompt: request.motionPrompt,
        duration_seconds: request.durationSeconds,
        fps: request.fps,
        chat_session_id: request.chatSessionId,
      }),
      signal,
    },
  )
  if (!response.ok) {
    const fallback = `创建视频草稿失败: HTTP ${response.status}`
    const body = await response.json().catch(() => null) as unknown
    const details = errorDetails(body, fallback)
    throw new SourceDraftVideoError(details.message, details.code, response.status)
  }
  return response.json() as Promise<SourceDraftVideoResponse>
}
