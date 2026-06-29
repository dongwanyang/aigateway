/**
 * API 客户端 — 与 API_CONTRACT.md 对齐
 *
 * 所有路径使用 VITE_API_BASE 环境变量拼接，禁止硬编码 /api/ 或 /admin/。
 */

import type {
  ApiResponse,
  ApiError,
  ChatCompletionRequest,
  ChatCompletionData,
  ChatCompletionChunkData,
  ModelListData,
  EmbeddingRequest,
  EmbeddingListData,
  ApiKeyListData,
  CreateApiKeyRequest,
  CreateApiKeyData,
  RevokedKeyData,
  DetailedQuotaData,
  HealthData,
  MetricSample,
} from '@/types'

// ------------------------------------------------------------------
// 基础配置
// ------------------------------------------------------------------

const API_BASE = import.meta.env.VITE_API_BASE ?? ''

async function ensureAuthHeaders(): Promise<Record<string, string>> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  // 从 localStorage 读取当前 API Key（由登录页或设置页写入）
  const apiKey = localStorage.getItem('aigateway_api_key')
  if (apiKey) {
    headers['Authorization'] = `Bearer ${apiKey}`
  }
  return headers
}

async function fetchJson<T>(
  path: string,
  options: RequestInit = {},
): Promise<{ data: T; message: string }> {
  const headers = await ensureAuthHeaders()
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: { ...headers, ...(options.headers ?? {}) },
  })

  if (!res.ok) {
    const body = (await res.json()) as ApiError
    const error = new Error(body.error.message)
    ;(error as any).code = body.error.code
    ;(error as any).status = res.status
    throw error
  }

  return res.json()
}

// ------------------------------------------------------------------
// Chat Completions
// ------------------------------------------------------------------

export async function createChatCompletion(
  body: ChatCompletionRequest,
): Promise<ApiResponse<ChatCompletionData>> {
  return fetchJson<ChatCompletionData>('/v1/chat/completions', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export async function createChatCompletionStream(
  body: ChatCompletionRequest,
): Promise<ReadableStream<ChatCompletionChunkData>> {
  const headers = await ensureAuthHeaders()
  const res = await fetch(`${API_BASE}/v1/chat/completions`, {
    method: 'POST',
    headers: { ...headers, 'Accept': 'text/event-stream' },
    body: JSON.stringify({ ...body, stream: true }),
  })

  if (!res.ok) {
    const body = (await res.json()) as ApiError
    throw new Error(body.error.message)
  }

  return res.body as unknown as ReadableStream<ChatCompletionChunkData>
}

// ------------------------------------------------------------------
// Models
// ------------------------------------------------------------------

export async function listModels(): Promise<ApiResponse<ModelListData>> {
  return fetchJson<ModelListData>('/v1/models')
}

// ------------------------------------------------------------------
// Embeddings
// ------------------------------------------------------------------

export async function createEmbeddings(body: EmbeddingRequest): Promise<ApiResponse<EmbeddingListData>> {
  return fetchJson<EmbeddingListData>('/v1/embeddings', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

// ------------------------------------------------------------------
// Admin: API Keys
// ------------------------------------------------------------------

export async function listApiKeys(
  page = 1,
  pageSize = 20,
): Promise<ApiResponse<ApiKeyListData>> {
  return fetchJson<ApiKeyListData>(
    `/admin/api-keys?page=${page}&pageSize=${pageSize}`,
  )
}

export async function createApiKey(
  body: CreateApiKeyRequest,
): Promise<ApiResponse<CreateApiKeyData>> {
  return fetchJson<CreateApiKeyData>('/admin/api-keys', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export async function deleteApiKey(keyId: string): Promise<ApiResponse<RevokedKeyData>> {
  return fetchJson<RevokedKeyData>(`/admin/api-keys/${encodeURIComponent(keyId)}`, {
    method: 'DELETE',
  })
}

// ------------------------------------------------------------------
// Admin: Quotas
// ------------------------------------------------------------------

export async function getQuota(keyId: string): Promise<ApiResponse<DetailedQuotaData>> {
  return fetchJson<DetailedQuotaData>(`/admin/quotas/${encodeURIComponent(keyId)}`)
}

// ------------------------------------------------------------------
// Health
// ------------------------------------------------------------------

export async function getHealth(): Promise<ApiResponse<HealthData>> {
  // 健康检查不需要鉴权
  const res = await fetch(`${API_BASE}/health`)
  if (!res.ok) throw new Error('Health check failed')
  return res.json()
}

// ------------------------------------------------------------------
// Metrics — 解析 Prometheus 文本格式
// ------------------------------------------------------------------

export async function getMetricsText(): Promise<string> {
  const res = await fetch(`${API_BASE}/metrics`)
  if (!res.ok) throw new Error('Failed to fetch metrics')
  return res.text()
}

export function parseMetrics(text: string): MetricSample[] {
  const samples: MetricSample[] = []
  for (const line of text.split('\n')) {
    if (!line.startsWith('gateway_') || line.startsWith('#')) continue
    const match = line.match(/^(.+?)\{(.+?)\} (.+)$/m)
    if (match) {
      const [, name, labelsStr, value] = match
      const labels: Record<string, string> = {}
      for (const pair of labelsStr.split(',')) {
        const [k, v] = pair.split('=').map(s => s.replace(/"/g, ''))
        if (k && v !== undefined) labels[k] = v
      }
      samples.push({ name, labels, value: parseFloat(value) })
    } else {
      const simpleMatch = line.match(/^(.+?) (.+)$/)
      if (simpleMatch) {
        const [, name, value] = simpleMatch
        samples.push({ name, labels: {}, value: parseFloat(value) })
      }
    }
  }
  return samples
}

// ------------------------------------------------------------------
// Admin: Metrics JSON
// ------------------------------------------------------------------

export interface MetricsJsonData {
  prometheus: Record<string, { labels: Record<string, string>; value: number }>
  keys: {
    total_keys: number
    total_daily_tokens_used: number
    total_monthly_cost_used: number
    total_requests: number
  }
  circuit_breakers: Record<string, unknown>
  uptime_seconds: number
}

export async function getMetricsJson(): Promise<ApiResponse<MetricsJsonData>> {
  const res = await fetch(`${API_BASE}/admin/metrics-json`)
  if (!res.ok) throw new Error('Failed to fetch metrics JSON')
  return res.json()
}

// ------------------------------------------------------------------
// Admin: Plugins Config
// ------------------------------------------------------------------

export interface PluginConfigItem {
  name: string
  enabled: boolean
  depends_on: string[]
  config: Record<string, unknown>
}

export interface PluginsConfigData {
  plugins: PluginConfigItem[]
}

export async function getPluginsConfig(): Promise<ApiResponse<PluginsConfigData>> {
  const res = await fetch(`${API_BASE}/admin/plugins-config`)
  if (!res.ok) throw new Error('Failed to fetch plugins config')
  return res.json()
}

export async function togglePlugin(name: string, enabled: boolean): Promise<ApiResponse<{ name: string; enabled: boolean }>> {
  const res = await fetch(`${API_BASE}/admin/plugins-config`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, enabled }),
  })
  if (!res.ok) throw new Error('Failed to toggle plugin')
  return res.json()
}

// ------------------------------------------------------------------
// Admin: Global Config (Hot Reload, Debug Mode)
// ------------------------------------------------------------------

export interface GlobalConfigData {
  hot_reload: boolean
  debug_mode: boolean
}

export async function getGlobalConfig(): Promise<ApiResponse<GlobalConfigData>> {
  const res = await fetch(`${API_BASE}/admin/global-config`)
  if (!res.ok) throw new Error('Failed to fetch global config')
  return res.json()
}

export async function updateGlobalConfig(config: { hot_reload: boolean; debug_mode: boolean }): Promise<ApiResponse<GlobalConfigData>> {
  const res = await fetch(`${API_BASE}/admin/global-config`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(config),
  })
  if (!res.ok) throw new Error('Failed to update global config')
  return res.json()
}

// ------------------------------------------------------------------
// Admin: Request Logs
// ------------------------------------------------------------------

export interface LogEntry {
  request_id: string
  trace_id: string
  user_id: string
  timestamp: number
  method: string
  endpoint: string
  model: string
  status: number
  duration_ms: number
  cache_hit: boolean
  tier: string | null
}

export interface LogsData {
  items: LogEntry[]
  pagination: { page: number; pageSize: number; total: number }
}

export async function getRequestLogs(params: {
  page?: number
  pageSize?: number
  user_id?: string
  model?: string
  status?: string
  cache_only?: boolean
}): Promise<ApiResponse<LogsData>> {
  const qs = new URLSearchParams()
  if (params.page) qs.set('page', String(params.page))
  if (params.pageSize) qs.set('pageSize', String(params.pageSize))
  if (params.user_id) qs.set('user_id', params.user_id)
  if (params.model) qs.set('model', params.model)
  if (params.status) qs.set('status', params.status)
  if (params.cache_only !== undefined) qs.set('cache_only', String(params.cache_only))
  const res = await fetch(`${API_BASE}/admin/logs?${qs}`)
  if (!res.ok) throw new Error('Failed to fetch logs')
  return res.json()
}
