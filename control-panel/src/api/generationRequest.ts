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

const MIN_POLL_DELAY_MS = 100
const MAX_POLL_DELAY_MS = 5_000
const CANCELLATION_TERMINAL_STATUSES = new Set([
  'completed',
  'confirmed',
  'expired',
  'failed',
  'rejected',
])

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

function errorStatus(error: unknown): number | undefined {
  const status = (error as Error & { status?: unknown })?.status
  return typeof status === 'number' ? status : undefined
}

function isRetryablePollError(error: unknown): boolean {
  if (error instanceof DOMException && error.name === 'AbortError') return false
  const status = errorStatus(error)
  return status === undefined || status === 429 || status >= 500
}

function pollDelay(state: GenerationRequestState | null, attempt: number): number {
  const serverDelay = state?.retry_after_ms
  if (typeof serverDelay === 'number' && Number.isFinite(serverDelay)) {
    return Math.min(MAX_POLL_DELAY_MS, Math.max(MIN_POLL_DELAY_MS, serverDelay))
  }
  const exponential = MIN_POLL_DELAY_MS * Math.pow(1.5, Math.min(attempt, 12))
  return Math.min(MAX_POLL_DELAY_MS, Math.round(exponential))
}

function delay(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException('Aborted', 'AbortError'))
      return
    }
    const onAbort = () => {
      window.clearTimeout(timer)
      reject(new DOMException('Aborted', 'AbortError'))
    }
    const timer = window.setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

function cancellationNotConfirmed(state: GenerationRequestState): Error {
  const error = new Error(`服务端未确认取消，当前状态: ${state.status}`)
  ;(error as Error & { code?: string }).code = 'generation_cancellation_not_confirmed'
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

/**
 * Resolve a response-lost generation until the server exposes a draft or a
 * terminal request state. Transient transport/5xx failures are retried until
 * the caller aborts; there is deliberately no short client-side timeout.
 */
export async function waitForGenerationRequestDraft(
  requestId: string,
  chatSessionId: string,
  signal?: AbortSignal,
): Promise<GenerationRequestState> {
  let attempt = 0
  let lastState: GenerationRequestState | null = null
  while (true) {
    try {
      lastState = await getGenerationRequest(requestId, chatSessionId, signal)
      if (lastState.draft_id || lastState.status === 'cancelled') return lastState
      await delay(pollDelay(lastState, attempt), signal)
      attempt += 1
    } catch (error) {
      if (signal?.aborted || !isRetryablePollError(error)) throw error
      await delay(pollDelay(lastState, attempt), signal)
      attempt += 1
    }
  }
}

/**
 * Request cancellation and do not report success until the persisted server
 * state is actually `cancelled`. A different terminal state is a cancellation
 * failure rather than a successful Stop operation.
 */
export async function cancelGenerationRequestAndWait(
  requestId: string,
  chatSessionId: string,
  signal?: AbortSignal,
): Promise<GenerationRequestState> {
  let state = await cancelGenerationRequest(requestId, chatSessionId)
  if (state.status === 'cancelled') return state

  let attempt = 0
  while (true) {
    if (CANCELLATION_TERMINAL_STATUSES.has(state.status)) {
      throw cancellationNotConfirmed(state)
    }
    await delay(pollDelay(state, attempt), signal)
    try {
      state = await getGenerationRequest(requestId, chatSessionId, signal)
      if (state.status === 'cancelled') return state
      attempt += 1
    } catch (error) {
      if (signal?.aborted || !isRetryablePollError(error)) throw error
      attempt += 1
    }
  }
}
