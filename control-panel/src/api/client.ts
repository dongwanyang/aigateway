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
  const apiKey = localStorage.getItem('aigateway_api_key')
  if (apiKey) {
    headers['Authorization'] = `Bearer ${apiKey}`
  }
  return headers
}

/** 保存 API Key 到 localStorage */
export function saveApiKey(key: string): void {
  localStorage.setItem('aigateway_api_key', key)
}

/** 清除已保存的 API Key */
export function clearApiKey(): void {
  localStorage.removeItem('aigateway_api_key')
}

/** 获取已保存的 API Key（不含） */
export function getSavedApiKey(): string | null {
  return localStorage.getItem('aigateway_api_key')
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
    `/admin/api-keys?page=${page}&page_size=${pageSize}`,
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

export interface UpdateQuotaRequest {
  daily_tokens?: number
  monthly_cost?: number
  rate_limit_rpm?: number
  rate_limit_tpm?: number
}

export interface UpdateQuotaData {
  id: string
  user_id: string
  quotas: {
    daily_tokens_limit: number
    monthly_cost_limit: number
    rate_limit_rpm: number
    rate_limit_tpm: number
  }
}

export async function updateApiKeyQuota(
  keyId: string,
  body: UpdateQuotaRequest,
): Promise<ApiResponse<UpdateQuotaData>> {
  return fetchJson<UpdateQuotaData>(`/admin/api-keys/${encodeURIComponent(keyId)}`, {
    method: 'PUT',
    body: JSON.stringify(body),
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
  if (!res.ok) throw new Error(`Health check failed: ${res.status}`)
  try {
    return await res.json()
  } catch {
    throw new Error('Health check returned invalid response')
  }
}

// ------------------------------------------------------------------
// Metrics — 解析 Prometheus 文本格式
// ------------------------------------------------------------------

export async function getMetricsText(): Promise<string> {
  const res = await fetch(`${API_BASE}/metrics`)
  if (!res.ok) throw new Error(`Failed to fetch metrics: ${res.status}`)
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
  const headers = await ensureAuthHeaders()
  const res = await fetch(`${API_BASE}/admin/metrics-json`, { headers })
  if (!res.ok) throw new Error(`Failed to fetch metrics JSON: ${res.status}`)
  try {
    return await res.json()
  } catch {
    throw new Error('Metrics JSON returned invalid response')
  }
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
  const headers = await ensureAuthHeaders()
  const res = await fetch(`${API_BASE}/admin/plugins-config`, { headers })
  if (!res.ok) throw new Error(`Failed to fetch plugins config: ${res.status}`)
  try {
    return await res.json()
  } catch {
    throw new Error('Plugins config returned invalid response')
  }
}

export async function togglePlugin(name: string, enabled: boolean): Promise<ApiResponse<{ name: string; enabled: boolean }>> {
  const headers = await ensureAuthHeaders()
  const res = await fetch(`${API_BASE}/admin/plugins-config`, {
    method: 'PUT',
    headers: { ...headers, 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, enabled }),
  })
  if (!res.ok) throw new Error(`Failed to toggle plugin: ${res.status}`)
  try {
    return await res.json()
  } catch {
    throw new Error('Toggle plugin returned invalid response')
  }
}

// ------------------------------------------------------------------
// Admin: Global Config (Hot Reload, Debug Mode)
// ------------------------------------------------------------------

export interface GlobalConfigData {
  hot_reload: boolean
  debug_mode: boolean
}

export async function getGlobalConfig(): Promise<ApiResponse<GlobalConfigData>> {
  const headers = await ensureAuthHeaders()
  const res = await fetch(`${API_BASE}/admin/global-config`, { headers })
  if (!res.ok) throw new Error(`Failed to fetch global config: ${res.status}`)
  try {
    return await res.json()
  } catch {
    throw new Error('Global config returned invalid response')
  }
}

export async function updateGlobalConfig(config: { hot_reload: boolean; debug_mode: boolean }): Promise<ApiResponse<GlobalConfigData>> {
  const headers = await ensureAuthHeaders()
  const res = await fetch(`${API_BASE}/admin/global-config`, {
    method: 'PUT',
    headers: { ...headers, 'Content-Type': 'application/json' },
    body: JSON.stringify(config),
  })
  if (!res.ok) throw new Error(`Failed to update global config: ${res.status}`)
  try {
    return await res.json()
  } catch {
    throw new Error('Update global config returned invalid response')
  }
}

// ------------------------------------------------------------------
// Admin: Request Logs
// ------------------------------------------------------------------

export interface PluginTraceStep {
  plugin_name: string
  duration_ms: number
  status: 'success' | 'skipped' | 'failed'
}

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
  plugin_trace?: PluginTraceStep[]
}

export interface TraceDetail {
  trace_id: string
  request_id: string
  user_id: string
  model: string
  endpoint: string
  status: number
  duration_ms: number
  cache_hit: boolean
  cache_tier: string | null
  timestamp: number
  plugin_trace: PluginTraceStep[]
  related_requests: LogEntry[]
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
  if (params.pageSize) qs.set('page_size', String(params.pageSize))
  if (params.user_id) qs.set('user_id', params.user_id)
  if (params.model) qs.set('model', params.model)
  if (params.status) qs.set('status', params.status)
  if (params.cache_only !== undefined) qs.set('cache_only', String(params.cache_only))
  const headers = await ensureAuthHeaders()
  const res = await fetch(`${API_BASE}/admin/logs?${qs}`, { headers })
  if (!res.ok) throw new Error(`Failed to fetch logs: ${res.status}`)
  try {
    return await res.json()
  } catch {
    throw new Error('Logs returned invalid response')
  }
}

export async function deleteAllLogs(): Promise<ApiResponse<{ deleted: boolean }>> {
  const headers = await ensureAuthHeaders()
  const res = await fetch(`${API_BASE}/admin/logs`, {
    method: 'DELETE',
    headers,
  })
  if (!res.ok) throw new Error(`Failed to delete logs: ${res.status}`)
  try {
    return await res.json()
  } catch {
    throw new Error('Delete logs returned invalid response')
  }
}

export async function getTraceDetail(traceId: string): Promise<ApiResponse<TraceDetail>> {
  const headers = await ensureAuthHeaders()
  const res = await fetch(`${API_BASE}/admin/trace/${encodeURIComponent(traceId)}`, { headers })
  if (!res.ok) throw new Error(`Failed to fetch trace: ${res.status}`)
  try {
    return await res.json()
  } catch {
    throw new Error('Trace detail returned invalid response')
  }
}


// ------------------------------------------------------------------
// Admin: Full Config (Req 15)
// ------------------------------------------------------------------

export async function getFullConfig(): Promise<ApiResponse<Record<string, unknown>>> {
  const headers = await ensureAuthHeaders()
  const res = await fetch(`${API_BASE}/admin/config`, { headers })
  if (!res.ok) throw new Error(`Failed to fetch config: ${res.status}`)
  return await res.json()
}

export async function updateFullConfig(config: Record<string, unknown>): Promise<ApiResponse<{ updated: boolean }>> {
  const headers = await ensureAuthHeaders()
  const res = await fetch(`${API_BASE}/admin/config`, {
    method: 'PUT',
    headers: { ...headers, 'Content-Type': 'application/json' },
    body: JSON.stringify(config),
  })
  if (!res.ok) throw new Error(`Failed to update config: ${res.status}`)
  return await res.json()
}

// ------------------------------------------------------------------
// Admin: RAG Documents (Req 18)
// ------------------------------------------------------------------

export interface RagDocument {
  doc_id: string
  filename: string
  file_type: string
  chunk_count: number
  chunk_strategy: string
  chunk_size: number
  chunk_overlap: number
  total_tokens: number
  created_at: number
  url: string
}

export async function listRagDocuments(): Promise<ApiResponse<{ documents: RagDocument[] }>> {
  const headers = await ensureAuthHeaders()
  const res = await fetch(`${API_BASE}/admin/rag/documents`, { headers })
  if (!res.ok) throw new Error(`Failed to fetch RAG documents: ${res.status}`)
  return await res.json()
}

export async function importRagDocument(params: {
  url?: string
  content?: string
  filename?: string
  chunk_strategy?: string
  chunk_size?: number
  chunk_overlap?: number
}): Promise<ApiResponse<{ doc_id: string; filename: string; chunk_count: number; total_tokens: number; elapsed_ms: number }>> {
  const headers = await ensureAuthHeaders()
  const res = await fetch(`${API_BASE}/admin/rag/documents`, {
    method: 'POST',
    headers: { ...headers, 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({ error: { message: `HTTP ${res.status}` } }))
    throw new Error(body.error?.message || `Failed: ${res.status}`)
  }
  return await res.json()
}

export async function deleteRagDocument(docId: string): Promise<ApiResponse<{ deleted: boolean }>> {
  const headers = await ensureAuthHeaders()
  const res = await fetch(`${API_BASE}/admin/rag/documents/${encodeURIComponent(docId)}`, {
    method: 'DELETE',
    headers,
  })
  if (!res.ok) throw new Error(`Failed to delete document: ${res.status}`)
  return await res.json()
}


// ------------------------------------------------------------------
// Admin: L3 Cache Lifecycle Management (Design §9b)
// ------------------------------------------------------------------

export interface L3CacheConfig {
  default_mode: 'auto' | 'manual'
  auto_cleanup_interval_minutes: number
  default_ttl_hours: number
  min_ttl_hours: number
  max_ttl_hours: number
}

export interface L3CacheEntry {
  id: string
  promptPreview: string
  model: string
  userId: string
  createdAt: number
  expiresAt: number | null
  mode: 'auto' | 'manual'
  hitCount: number
  tokenCount: number
}

export interface L3EntriesData {
  items: L3CacheEntry[]
  pagination: { page: number; pageSize: number; total: number }
}

export async function getL3CacheConfig(): Promise<ApiResponse<L3CacheConfig>> {
  return fetchJson<L3CacheConfig>('/admin/cache/l3/config')
}

export async function updateL3CacheConfig(config: Partial<L3CacheConfig>): Promise<ApiResponse<L3CacheConfig>> {
  return fetchJson<L3CacheConfig>('/admin/cache/l3/config', {
    method: 'PUT',
    body: JSON.stringify(config),
  })
}

export async function listL3Entries(params: {
  page?: number
  pageSize?: number
  mode?: string
  userId?: string
  sortBy?: string
}): Promise<ApiResponse<L3EntriesData>> {
  const qs = new URLSearchParams()
  if (params.page) qs.set('page', String(params.page))
  if (params.pageSize) qs.set('page_size', String(params.pageSize))
  if (params.mode) qs.set('mode', params.mode)
  if (params.userId) qs.set('user_id', params.userId)
  if (params.sortBy) qs.set('sort_by', params.sortBy)
  return fetchJson<L3EntriesData>(`/admin/cache/l3/entries?${qs}`)
}

export async function updateL3EntryMode(pointId: string, mode: 'auto' | 'manual', ttlHours?: number): Promise<ApiResponse<{ point_id: string; mode: string; ttl: number }>> {
  const body: Record<string, unknown> = { mode }
  if (ttlHours !== undefined) body.ttl_hours = ttlHours
  return fetchJson<{ point_id: string; mode: string; ttl: number }>(`/admin/cache/l3/entries/${encodeURIComponent(pointId)}/mode`, {
    method: 'PUT',
    body: JSON.stringify(body),
  })
}

export async function deleteL3Entry(pointId: string): Promise<ApiResponse<{ point_id: string; deleted: boolean }>> {
  return fetchJson<{ point_id: string; deleted: boolean }>(`/admin/cache/l3/entries/${encodeURIComponent(pointId)}`, {
    method: 'DELETE',
  })
}

export async function triggerL3Cleanup(): Promise<ApiResponse<{ deleted_count: number }>> {
  return fetchJson<{ deleted_count: number }>('/admin/cache/l3/cleanup', {
    method: 'POST',
  })
}

// ------------------------------------------------------------------
// Admin: Provider Connectivity Test & Model List
// ------------------------------------------------------------------

export interface ConnectivityTestResult {
  provider: string
  success: boolean
  latency_ms: number
  error?: string
}

export async function testProviderConnectivity(provider: string): Promise<ApiResponse<ConnectivityTestResult>> {
  return fetchJson<ConnectivityTestResult>(`/admin/providers/${encodeURIComponent(provider)}/test`, {
    method: 'POST',
  })
}

export interface ProviderModelsResult {
  provider: string
  models: string[]
}

export async function fetchProviderModels(provider: string): Promise<ApiResponse<ProviderModelsResult>> {
  return fetchJson<ProviderModelsResult>(`/admin/providers/${encodeURIComponent(provider)}/models`)
}
