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

function errorMessage(body: unknown, fallback: string): string {
  if (!body || typeof body !== 'object') return fallback
  const value = body as {
    error?: { code?: string; message?: string }
    detail?: { error?: { code?: string; message?: string } } | string
  }
  if (typeof value.detail === 'string') return value.detail
  return value.error?.message
    ?? value.error?.code
    ?? (typeof value.detail === 'object' ? value.detail?.error?.message : undefined)
    ?? (typeof value.detail === 'object' ? value.detail?.error?.code : undefined)
    ?? fallback
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
    const body = await response.json().catch(() => null) as unknown
    throw new Error(errorMessage(body, `创建视频草稿失败: HTTP ${response.status}`))
  }
  return response.json() as Promise<SourceDraftVideoResponse>
}
