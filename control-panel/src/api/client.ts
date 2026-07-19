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
  Group,
  GroupListData,
  CreateGroupRequest,
  UpdateGroupRequest,
  AssignGroupRequest,
  CacheScope,
  VideoStatusResponse,
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

/**
 * POST /v1/chat/completions (stream=true) —— 返回原始字节流。
 *
 * 返回 `ReadableStream<Uint8Array>`(fetch res.body 的真实类型),由调用方用
 * TextDecoder 自行解析 SSE 帧。`signal` 透传给底层 fetch,使调用方能真正取消
 * 上游请求(否则只 abort 读循环、fetch 仍跑到结束,白扣 token / 配额)。
 */
export async function createChatCompletionStream(
  body: ChatCompletionRequest,
  signal?: AbortSignal,
): Promise<ReadableStream<Uint8Array>> {
  const headers = await ensureAuthHeaders()
  const res = await fetch(`${API_BASE}/v1/chat/completions`, {
    method: 'POST',
    headers: { ...headers, 'Accept': 'text/event-stream' },
    body: JSON.stringify({ ...body, stream: true }),
    signal,
  })

  if (!res.ok) {
    let errorMsg = `HTTP ${res.status}`
    try {
      const body = (await res.json()) as ApiError
      errorMsg = body.error?.message || errorMsg
    } catch {
      // Non-JSON error response (e.g. HTML nginx page); use status code
    }
    throw new Error(errorMsg)
  }

  if (!res.body) {
    throw new Error('Streaming response has no body')
  }
  return res.body
}

/** GET /v1/videos/{id} —— 轮询视频生成任务状态(passthrough 上游 JSON)。 */
export async function getVideoStatus(videoId: string): Promise<VideoStatusResponse> {
  const headers = await ensureAuthHeaders()
  const res = await fetch(`${API_BASE}/v1/videos/${encodeURIComponent(videoId)}`, {
    headers,
  })
  if (!res.ok) {
    throw new Error(`视频状态查询失败: HTTP ${res.status}`)
  }
  return (await res.json()) as VideoStatusResponse
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
    const match = line.match(/^(.+?)\{(.+?)\} (.+)$/)
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
// Admin: Cost Ledger (SQLite 持久化)
// ------------------------------------------------------------------

export interface LedgerRow {
  id: number
  trace_id: string
  ts: string
  ts_unix: number
  user_id: string
  group_id: string
  model: string
  provider: string
  pipeline_kind: string
  tokens_in: number
  tokens_out: number
  tokens_total: number
  cost_usd: number
  cached: number
  stream: number
  status: string
}

export interface CostSummary {
  total: Record<string, number>
  by_model: AggregateRow[]
  by_user: AggregateRow[]
  by_group: AggregateRow[]
  by_day: AggregateDayRow[]
}

interface AggregateRow {
  k: string
  requests: number
  tokens_in: number
  tokens_out: number
  tokens_total: number
  cost_usd: number
  cache_hits: number
}

interface AggregateDayRow {
  k: string
  requests: number
  tokens_total: number
  cost_usd: number
}

export async function getCostLedger(params?: {
  limit?: number
  offset?: number
  start?: number | null
  end?: number | null
  user_id?: string | null
  group_id?: string | null
  model?: string | null
}): Promise<LedgerRow[]> {
  const headers = await ensureAuthHeaders()
  const qs = new URLSearchParams()
  if (params?.limit) qs.set('limit', String(params.limit))
  if (params?.offset) qs.set('offset', String(params.offset))
  if (params?.start !== undefined && params.start !== null) qs.set('start', String(params.start))
  if (params?.end !== undefined && params.end !== null) qs.set('end', String(params.end))
  if (params?.user_id) qs.set('user_id', params.user_id)
  if (params?.group_id) qs.set('group_id', params.group_id)
  if (params?.model) qs.set('model', params.model)
  const url = `${API_BASE}/admin/costs/ledger${qs.toString() ? '?' + qs.toString() : ''}`
  const res = await fetch(url, { headers })
  if (!res.ok) throw new Error(`Failed to fetch cost ledger: ${res.status}`)
  const body = await res.json()
  return body.rows ?? []
}

export async function getCostSummary(days?: number): Promise<CostSummary> {
  const headers = await ensureAuthHeaders()
  const qs = days ? `?days=${days}` : ''
  const res = await fetch(`${API_BASE}/admin/costs/summary${qs}`, { headers })
  if (!res.ok) throw new Error(`Failed to fetch cost summary: ${res.status}`)
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
  pipeline_kind?: 'understanding' | 'generation'
  priority?: number
  /** 该插件 debug 开关当前值;null 表示不支持单独 debug(如 prompt_compress 归 entry 维度) */
  debug?: boolean | null
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

export async function updateGlobalConfig(config: { hot_reload: boolean; debug_mode?: boolean }): Promise<ApiResponse<GlobalConfigData>> {
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

export interface TraceEvent {
  trace_id: string
  ts: number
  stage: string
  kind: string
  name: string
  duration_ms: number
  status: string
  payload?: Record<string, unknown> | null
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
  events: TraceEvent[]
  plugin_trace: PluginTraceStep[]
  related_requests: LogEntry[]
  meta?: { wall_start?: number } | null
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

export async function batchDeleteLogs(requestIds: string[]): Promise<ApiResponse<{ deleted: number; requested: number }>> {
  const headers = await ensureAuthHeaders()
  const res = await fetch(`${API_BASE}/admin/logs/batch-delete`, {
    method: 'POST',
    headers: { ...headers, 'Content-Type': 'application/json' },
    body: JSON.stringify({ request_ids: requestIds }),
  })
  if (!res.ok) throw new Error(`Failed to batch-delete logs: ${res.status}`)
  try {
    return await res.json()
  } catch {
    throw new Error('Batch delete returned invalid response')
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
// Admin: Code RAG (Task 3-6, 代码知识库)
// ------------------------------------------------------------------

export type CodeImportSourceType = 'folder' | 'server_path' | 'git' | 'zip'

export type CodeImportTaskStatus =
  | 'pending'
  | 'scanning'
  | 'splitting'
  | 'building_graph'
  | 'embedding'
  | 'completed'
  | 'failed'
  | 'cancelled'

export interface CodeImportTask {
  task_id: string
  status: CodeImportTaskStatus
  current_file: string | null
  done: number
  total: number
  error: string | null
  source_label: string | null
  source_type: string | null
  created_at: number
}

export interface CodeRepositoryImport {
  document_id: string
  source_type: CodeImportSourceType
  source_label: string
  file_count: number
  language_summary: string[]
  function_count: number
  class_count: number
  chunk_count: number
  embedding_model: string
  import_time: string
}

export type CodeImportJsonPayload =
  | { source_type: 'server_path'; server_path: string; embedding_model: string }
  | { source_type: 'git'; git_url: string; git_branch?: string; embedding_model: string }

export async function importCodeRepository(
  payload: FormData | CodeImportJsonPayload,
): Promise<{ task_id: string; status: 'pending' }> {
  const headers = await ensureAuthHeaders()
  // FormData 必须让浏览器自动设置 Content-Type (含 multipart boundary).
  // ensureAuthHeaders 默认带了 'Content-Type: application/json', 会覆盖浏览器的
  // multipart/form-data; boundary=..., 导致后端按 JSON 解析二进制 body →
  // "'utf-8' codec can't decode byte 0xfb" / "Expecting value: line 1 column 1".
  const init: RequestInit =
    payload instanceof FormData
      ? {
          method: 'POST',
          // 只透传 Authorization, 删掉默认的 application/json, 让浏览器补 boundary
          headers: { Authorization: headers['Authorization'] || '' },
          body: payload,
        }
      : {
          method: 'POST',
          headers: { ...headers, 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        }
  const res = await fetch(`${API_BASE}/admin/rag/code/import`, init)
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }))
    throw new Error(
      typeof body?.detail === 'string' ? body.detail : `Code import failed: ${res.status}`,
    )
  }
  return await res.json()
}

export async function listCodeImportTasks(): Promise<CodeImportTask[]> {
  const headers = await ensureAuthHeaders()
  const res = await fetch(`${API_BASE}/admin/rag/code/tasks`, { headers })
  if (!res.ok) throw new Error(`Failed to list code import tasks: ${res.status}`)
  return await res.json()
}

export async function getCodeImportTask(taskId: string): Promise<CodeImportTask> {
  const headers = await ensureAuthHeaders()
  const res = await fetch(
    `${API_BASE}/admin/rag/code/tasks/${encodeURIComponent(taskId)}`,
    { headers },
  )
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    // FastAPI HTTPException(detail=str) → body.detail = string
    // FastAPI HTTPException(detail=dict) → body = dict (detail lifted to top level)
    const msg = typeof body?.detail === 'string'
      ? body.detail
      : typeof body?.message === 'string'
        ? body.message
        : typeof body?.detail?.error?.message === 'string'
          ? body.detail.error.message
          : `Failed to fetch code import task: ${res.status}`
    throw new Error(msg)
  }
  return await res.json()
}

export async function cancelCodeImportTask(taskId: string): Promise<{ task_id: string; status: CodeImportTaskStatus }> {
  const headers = await ensureAuthHeaders()
  const res = await fetch(
    `${API_BASE}/admin/rag/code/tasks/${encodeURIComponent(taskId)}/cancel`,
    { method: 'POST', headers },
  )
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }))
    const message = typeof body?.detail === 'string'
      ? body.detail
      : body?.detail?.error?.message || `Failed to cancel code import task: ${res.status}`
    throw new Error(message)
  }
  return await res.json()
}

export async function listCodeRepositories(): Promise<CodeRepositoryImport[]> {
  const headers = await ensureAuthHeaders()
  const res = await fetch(`${API_BASE}/admin/rag/code/repositories`, { headers })
  if (!res.ok) throw new Error(`Failed to list code repositories: ${res.status}`)
  return await res.json()
}

export async function deleteCodeRepository(documentId: string): Promise<void> {
  const headers = await ensureAuthHeaders()
  const res = await fetch(
    `${API_BASE}/admin/rag/code/repositories/${encodeURIComponent(documentId)}`,
    { method: 'DELETE', headers },
  )
  if (!res.ok) throw new Error(`Failed to delete code repository: ${res.status}`)
}

// --- Code RAG graph query / sync (重构后:走 codegraph CLI 的查询端点) ---

export interface CodeSymbolNode {
  id: string | null
  kind: string | null
  name: string | null
  qualified_name: string | null
  file_path: string | null
  language: string | null
  start_line: number | null
  end_line: number | null
  signature: string | null
  docstring: string | null
}

export interface CodeSymbolRef {
  name: string | null
  kind: string | null
  file_path: string | null
  start_line: number | null
}

export interface CodeFileSyncResult {
  document_id: string
  synced_files: number
  refreshed_symbols: number
  deleted_files?: number
}

export async function syncCodeRepository(documentId: string): Promise<CodeFileSyncResult> {
  const headers = await ensureAuthHeaders()
  const res = await fetch(
    `${API_BASE}/admin/rag/code/repositories/${encodeURIComponent(documentId)}/sync`,
    { method: 'POST', headers },
  )
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }))
    throw new Error(
      typeof body?.detail === 'string' ? body.detail : `Code sync failed: ${res.status}`,
    )
  }
  return await res.json()
}

export async function queryCodeSymbols(
  documentId: string,
  symbol: string,
  opts?: { kind?: string; limit?: number },
): Promise<CodeSymbolNode[]> {
  const headers = await ensureAuthHeaders()
  const params = new URLSearchParams({ symbol, limit: String(opts?.limit ?? 10) })
  if (opts?.kind) params.set('kind', opts.kind)
  const res = await fetch(
    `${API_BASE}/admin/rag/code/repositories/${encodeURIComponent(documentId)}/query?${params}`,
    { headers },
  )
  if (!res.ok) throw new Error(`Code query failed: ${res.status}`)
  return await res.json()
}

export async function getCodeCallers(documentId: string, symbol: string): Promise<CodeSymbolRef[]> {
  const headers = await ensureAuthHeaders()
  const params = new URLSearchParams({ symbol })
  const res = await fetch(
    `${API_BASE}/admin/rag/code/repositories/${encodeURIComponent(documentId)}/callers?${params}`,
    { headers },
  )
  if (!res.ok) throw new Error(`Code callers failed: ${res.status}`)
  const body = await res.json()
  return body?.callers ?? []
}

export async function getCodeCallees(documentId: string, symbol: string): Promise<CodeSymbolRef[]> {
  const headers = await ensureAuthHeaders()
  const params = new URLSearchParams({ symbol })
  const res = await fetch(
    `${API_BASE}/admin/rag/code/repositories/${encodeURIComponent(documentId)}/callees?${params}`,
    { headers },
  )
  if (!res.ok) throw new Error(`Code callees failed: ${res.status}`)
  const body = await res.json()
  return body?.callees ?? []
}

export async function getCodeImpact(
  documentId: string,
  symbol: string,
  depth = 2,
): Promise<CodeSymbolRef[]> {
  const headers = await ensureAuthHeaders()
  const params = new URLSearchParams({ symbol, depth: String(depth) })
  const res = await fetch(
    `${API_BASE}/admin/rag/code/repositories/${encodeURIComponent(documentId)}/impact?${params}`,
    { headers },
  )
  if (!res.ok) throw new Error(`Code impact failed: ${res.status}`)
  const body = await res.json()
  return body?.affected ?? []
}


// ------------------------------------------------------------------
// Code RAG: File list + full symbol listing (call-graph panel)
// ------------------------------------------------------------------

export interface CodeFile {
  path: string
  language: string
  node_count: number | null
  size: number | null
}

export async function listCodeFiles(documentId: string): Promise<CodeFile[]> {
  const headers = await ensureAuthHeaders()
  const res = await fetch(
    `${API_BASE}/admin/rag/code/repositories/${encodeURIComponent(documentId)}/files`,
    { headers },
  )
  if (!res.ok) throw new Error(`Code files failed: ${res.status}`)
  return await res.json()
}

// List all symbols via empty-string search (no new backend endpoint needed).
// codegraph CLI ignores --limit partially on empty search, so pass a large limit
// to fetch the full set; caller checks length === limit to detect truncation.
export async function listAllSymbols(
  documentId: string,
  opts?: { kind?: string; limit?: number },
): Promise<CodeSymbolNode[]> {
  const headers = await ensureAuthHeaders()
  const limit = opts?.limit ?? 5000
  const params = new URLSearchParams({ symbol: '', limit: String(limit) })
  if (opts?.kind) params.set('kind', opts.kind)
  const res = await fetch(
    `${API_BASE}/admin/rag/code/repositories/${encodeURIComponent(documentId)}/query?${params}`,
    { headers },
  )
  if (!res.ok) throw new Error(`List symbols failed: ${res.status}`)
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

// ------------------------------------------------------------------
// Debug 开关(5 维度 + per_plugin) — PR2/PR3 2026-07-05
// ------------------------------------------------------------------

export interface DebugConfig {
  frontend: boolean
  entry: boolean
  cache: boolean
  bridge: boolean
  plugins_enabled: boolean
  per_plugin: Record<string, boolean>
}

/** 读当前 DebugConfig(5 维度 + per_plugin)。 */
export async function getDebugConfig(): Promise<DebugConfig> {
  const { data } = await fetchJson<DebugConfig>('/admin/config/debug')
  return data
}

/** 开关单个插件的 debug 日志(写 config.yaml debug.plugins.per_plugin[name])。 */
export async function setPluginDebug(pluginName: string, enabled: boolean): Promise<void> {
  await fetchJson<{ plugin: string; debug: boolean }>(
    `/admin/plugins/${encodeURIComponent(pluginName)}/debug`,
    { method: 'POST', body: JSON.stringify({ enabled }) },
  )
}

/**
 * 更新 debug 段的若干维度(整段覆盖写)。
 * 传 Partial<DebugConfig>,只覆盖给出的字段;后端用 yaml.dump 整段写回。
 */
export async function updateDebugSection(debug: Partial<DebugConfig>): Promise<void> {
  // 复用 /admin/global-config PUT(Task 13 已支持 debug 字段整段覆盖)。
  // 后端期望完整 body:hot_reload + debug_mode 必填(raw.get(...,False) 否则被覆盖为 False),
  // 这里读不到当前值,故先 GET 再 PUT,避免误覆盖 hot_reload/debug_mode。
  const cur = await fetchJson<{ hot_reload: boolean; debug_mode: boolean; debug: unknown }>(
    '/admin/global-config',
  )
  await fetchJson<{ hot_reload: boolean; debug_mode: boolean; debug: unknown }>(
    '/admin/global-config',
    {
      method: 'PUT',
      body: JSON.stringify({
        hot_reload: cur.data.hot_reload,
        debug_mode: cur.data.debug_mode,
        debug: { ...((cur.data.debug as object) ?? {}), ...debug },
      }),
    },
  )
}

// ------------------------------------------------------------------
// User Groups
// ------------------------------------------------------------------

export async function listGroups(): Promise<ApiResponse<GroupListData>> {
  return fetchJson<GroupListData>('/admin/groups')
}

export async function createGroup(body: CreateGroupRequest): Promise<ApiResponse<Group>> {
  return fetchJson<Group>('/admin/groups', { method: 'POST', body: JSON.stringify(body) })
}

export async function getGroup(groupId: string): Promise<ApiResponse<Group>> {
  return fetchJson<Group>(`/admin/groups/${encodeURIComponent(groupId)}`)
}

export async function updateGroup(
  groupId: string,
  body: UpdateGroupRequest,
): Promise<ApiResponse<Group>> {
  return fetchJson<Group>(`/admin/groups/${encodeURIComponent(groupId)}`, {
    method: 'PUT',
    body: JSON.stringify(body),
  })
}

export async function deleteGroup(
  groupId: string,
): Promise<ApiResponse<{ id: string; status: string }>> {
  return fetchJson<{ id: string; status: string }>(`/admin/groups/${encodeURIComponent(groupId)}`, {
    method: 'DELETE',
  })
}

export async function assignKeyGroup(
  keyId: string,
  groupId: string,
  cacheScope?: CacheScope,
): Promise<ApiResponse<unknown>> {
  return fetchJson<unknown>(`/admin/api-keys/${encodeURIComponent(keyId)}/group`, {
    method: 'PUT',
    body: JSON.stringify({ group_id: groupId, cache_scope: cacheScope } as AssignGroupRequest),
  })
}

// ------------------------------------------------------------------
// Prometheus range-query proxy
// ------------------------------------------------------------------

export interface PromQueryValue {
  timestamp: string
  value: string
}

export interface PromQueryResult {
  status: string
  data: {
    resultType: string
    result: Array<{ metric: Record<string, string>; values: PromQueryValue[] }>
  }
}

export async function metricsQuery(params: {
  query: string
  start?: string
  end?: string
  step?: string
}): Promise<PromQueryResult> {
  const qs = new URLSearchParams({ query: params.query, step: params.step || '3600' })
  if (params.start) qs.set('start', params.start)
  if (params.end) qs.set('end', params.end)
  const resp = await fetch(`${API_BASE}/admin/metrics/query_range?${qs}`, {
    headers: await ensureAuthHeaders(),
  })
  if (!resp.ok) throw new Error(`Prometheus query failed: ${resp.status}`)
  return resp.json()
}
