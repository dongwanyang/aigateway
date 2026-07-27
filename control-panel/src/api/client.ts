/**
 * API 客户端 — 与 API_CONTRACT.md 对齐
 *
 * 所有路径使用 VITE_API_BASE 环境变量拼接，禁止硬编码 /api/ 或 /admin/。
 * 控制台认证逻辑集中在 authSession.ts；本文件不再导出登录/登出/重置密码 helper。
 */

import type {
  ApiResponse,
  ApiError,
  ChatCompletionRequest,
  ChatCompletionData,
} from '@/types'

// ------------------------------------------------------------------
// 基础配置
// ------------------------------------------------------------------

const API_BASE = import.meta.env.VITE_API_BASE ?? ''

async function ensureAuthHeaders(): Promise<Record<string, string>> {
  return { 'Content-Type': 'application/json' }
}

async function fetchJson<T>(
  path: string,
  options: RequestInit = {},
): Promise<{ data: T; message: string }> {
  const headers = await ensureAuthHeaders()
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    credentials: 'include',
    headers: { ...headers, ...(options.headers ?? {}) },
  })

  if (!res.ok) {
    let code = 'unknown_error'
    let message = `HTTP ${res.status}`
    try {
      const body = (await res.json()) as ApiError
      code = body.error?.code ?? code
      message = body.error?.message ?? message
    } catch {
      // Response body is not valid JSON (e.g., nginx 502 HTML page)
      message = `Server error: ${res.status} ${res.statusText}`
    }
    const error = new Error(message)
    ;(error as any).code = code
    ;(error as any).status = res.status
    throw error
  }

  return res.json()
}

// ------------------------------------------------------------------
// Control-panel Chat
// ------------------------------------------------------------------

const CONSOLE_CHAT_COMPLETIONS_PATH = '/admin/console/chat/completions'

export async function createChatCompletion(
  body: ChatCompletionRequest,
): Promise<ApiResponse<ChatCompletionData>> {
  return fetchJson<ChatCompletionData>(CONSOLE_CHAT_COMPLETIONS_PATH, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

/**
 * POST /admin/console/chat/completions (stream=true) —— 按 content-type 分流返回。
 *
 * `/v1/*` 现在只接受 API Key header，避免浏览器 cookie 绕过 API Key 配额与
 * 成本账本。控制台聊天走 admin-scoped endpoint，由后端将已登录 browser session
 * 绑定到服务端 API Key 后再进入 RequestDispatcher。
 *
 * 后端对 understanding 意图返回 text/event-stream(逐 token);对 generation
 * 意图命中草稿门控时直接返回 application/json(`draft_pending_confirmation`)。
 * 调用方据 kind 分流:stream 走 SSE 解析,draft 直接消费草稿元数据。
 *
 * `signal` 透传给底层 fetch,使调用方能真正取消上游请求。
 */
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
  const res = await fetch(`${API_BASE}${CONSOLE_CHAT_COMPLETIONS_PATH}`, {
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

  // 草稿门控:application/json + draft_pending_confirmation
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

  // 流式 understanding
  if (!res.body) {
    throw new Error('Streaming response has no body')
  }
  return { kind: 'stream', body: res.body }
}
