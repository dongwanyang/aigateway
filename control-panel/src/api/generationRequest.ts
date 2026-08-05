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
const REQUEST_REGISTRATION_GRACE_MS = 10_000
const REQUEST_RECOVERY_TERMINAL_STATUSES = new Set([
  'cancelled',
  'non_draft',
  'failed',
])
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

function terminalStateError(
  code: string,
  message: string,
  status: number,
): Error {
  const error = new Error(message)
  ;(error as Error & { code?: string; status?: number }).code = code
  ;(error as Error & { code?: string; status?: number }).status = status
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

function assertRegistrationGrace(
  state: GenerationRequestState,
  unregisteredElapsedMs: number,
): void {
  if (
    state.status === 'unregistered'
    && unregisteredElapsedMs >= REQUEST_REGISTRATION_GRACE_MS
  ) {
    throw terminalStateError(
      'generation_request_not_registered',
      '生成请求未到达服务端，请重新提交',
      404,
    )
  }
}

function elapsedAfterDelay(
  state: GenerationRequestState | null,
  current: number,
  delayMs: number,
): number {
  return state?.status === 'unregistered' ? current + delayMs : 0
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

/** Wait until the server exposes a draft or an explicit terminal request state. */
export async function waitForGenerationRequestState(
  requestId: string,
  chatSessionId: string,
  signal?: AbortSignal,
): Promise<GenerationRequestState> {
  let attempt = 0
  let lastState: GenerationRequestState | null = null
  let unregisteredElapsedMs = 0
  while (true) {
    try {
      lastState = await getGenerationRequest(requestId, chatSessionId, signal)
    } catch (error) {
      if (signal?.aborted || !isRetryablePollError(error)) throw error
      const retryDelay = pollDelay(lastState, attempt)
      await delay(retryDelay, signal)
      unregisteredElapsedMs = elapsedAfterDelay(
        lastState,
        unregisteredElapsedMs,
        retryDelay,
      )
      attempt += 1
      continue
    }

    if (lastState.status !== 'unregistered') unregisteredElapsedMs = 0
    assertRegistrationGrace(lastState, unregisteredElapsedMs)
    if (
      lastState.draft_id
      || REQUEST_RECOVERY_TERMINAL_STATUSES.has(lastState.status)
    ) return lastState

    const retryDelay = pollDelay(lastState, attempt)
    await delay(retryDelay, signal)
    unregisteredElapsedMs = elapsedAfterDelay(
      lastState,
      unregisteredElapsedMs,
      retryDelay,
    )
    attempt += 1
  }
}

/**
 * Resolve a response-lost generation that is required to produce a draft.
 * Non-draft and failed terminal records are explicit errors so page-refresh
 * recovery cannot leave an assistant message permanently awaiting a draft.
 */
export async function waitForGenerationRequestDraft(
  requestId: string,
  chatSessionId: string,
  signal?: AbortSignal,
): Promise<GenerationRequestState> {
  const state = await waitForGenerationRequestState(
    requestId,
    chatSessionId,
    signal,
  )
  if (state.status === 'non_draft') {
    throw terminalStateError(
      'generation_request_not_draft',
      '该请求是普通文本响应，断开的响应内容无法恢复',
      409,
    )
  }
  if (state.status === 'failed') {
    throw terminalStateError(
      state.error || 'generation_request_failed',
      '生成请求在服务端执行失败',
      502,
    )
  }
  return state
}

/**
 * Request cancellation and do not report success until the persisted server
 * state is actually cancelled. A non-draft terminal record is also successful:
 * aborting its response transport leaves no detached GPU/background task.
 */
export async function cancelGenerationRequestAndWait(
  requestId: string,
  chatSessionId: string,
  signal?: AbortSignal,
): Promise<GenerationRequestState> {
  let state = await cancelGenerationRequest(requestId, chatSessionId)
  let attempt = 0
  let unregisteredElapsedMs = 0

  while (true) {
    if (state.status !== 'unregistered') unregisteredElapsedMs = 0
    assertRegistrationGrace(state, unregisteredElapsedMs)
    if (state.status === 'cancelled') return state
    if (state.status === 'non_draft') {
      return { ...state, status: 'cancelled', stage: state.stage ?? 'transport_cancelled' }
    }
    if (CANCELLATION_TERMINAL_STATUSES.has(state.status)) {
      throw cancellationNotConfirmed(state)
    }

    const retryDelay = pollDelay(state, attempt)
    await delay(retryDelay, signal)
    unregisteredElapsedMs = elapsedAfterDelay(
      state,
      unregisteredElapsedMs,
      retryDelay,
    )
    try {
      state = await getGenerationRequest(requestId, chatSessionId, signal)
    } catch (error) {
      if (signal?.aborted || !isRetryablePollError(error)) throw error
    }
    attempt += 1
  }
}
