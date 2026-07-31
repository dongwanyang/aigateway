/**
 * API 客户端 — 与 API_CONTRACT.md 对齐
 * 控制台认证逻辑集中在 authSession.ts；本文件只保留已登录后的普通资源 API。
 */
import type { ApiResponse, ApiError, ChatCompletionRequest, ChatCompletionData, ModelListData, EmbeddingRequest, EmbeddingListData, ApiKeyListData, CreateApiKeyRequest, CreateApiKeyData, RevokedKeyData, DetailedQuotaData, HealthData, MetricSample, Group, GroupListData, CreateGroupRequest, UpdateGroupRequest, CacheScope, VideoStatusResponse } from '@/types'
export { requestChatCompletion, type ChatResponse } from './consoleChat'

const API_BASE = import.meta.env.VITE_API_BASE ?? ''
async function ensureAuthHeaders(): Promise<Record<string, string>> { return { 'Content-Type': 'application/json' } }
async function fetchJson<T>(path: string, options: RequestInit = {}): Promise<{ data: T; message: string }> {
  const headers = await ensureAuthHeaders()
  const res = await fetch(`${API_BASE}${path}`, { ...options, credentials: 'include', headers: { ...headers, ...(options.headers ?? {}) } })
  if (!res.ok) { let code = 'unknown_error'; let message = `HTTP ${res.status}`; try { const body = (await res.json()) as ApiError; code = body.error?.code ?? code; message = body.error?.message ?? message } catch { message = `Server error: ${res.status} ${res.statusText}` }; const error = new Error(message); ;(error as any).code = code; ;(error as any).status = res.status; throw error }
  return res.json()
}
async function rawJson<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = await ensureAuthHeaders()
  const res = await fetch(`${API_BASE}${path}`, { ...options, credentials: 'include', headers: { ...headers, ...(options.headers ?? {}) } })
  if (!res.ok) { const body = await res.json().catch(() => ({})); const msg = body?.error?.message ?? body?.detail?.error?.message ?? body?.detail ?? `HTTP ${res.status}`; throw new Error(String(msg)) }
  // 204 No Content (空响应体) — 跳过 res.json()，否则抛 SyntaxError 把成功误判为失败。
  if (res.status === 204) return undefined as T
  return res.json()
}
async function errorText(res: Response, fallback: string): Promise<string> { try { const body = await res.json(); return body?.error?.code || body?.error?.message || body?.detail?.error?.code || body?.detail?.error?.message || body?.detail || `${fallback}: HTTP ${res.status}` } catch { return `${fallback}: HTTP ${res.status}` } }

export async function createChatCompletion(body: ChatCompletionRequest): Promise<ApiResponse<ChatCompletionData>> { return fetchJson<ChatCompletionData>('/admin/console/chat/completions', { method: 'POST', body: JSON.stringify(body) }) }
export async function getDraftPreview(draftId: string): Promise<{ previewDataUrl?: string; previewCount?: number; status?: string; stage?: string; progress?: number; progressSource?: string }> { const headers = await ensureAuthHeaders(); const res = await fetch(`${API_BASE}/admin/draft/${encodeURIComponent(draftId)}/preview`, { credentials: 'include', headers }); if (res.status === 202) { const json = await res.json().catch(() => ({})) as { status?: string; stage?: string; progress?: number; progress_source?: string }; return { status: json.status ?? 'running', stage: json.stage, progress: json.progress, progressSource: json.progress_source } }; if (!res.ok) throw new Error(await errorText(res, 'preview 加载失败')); const json = await res.json() as { preview_data_url?: string; preview_count?: number }; if (!json.preview_data_url) throw new Error('preview 响应缺少 preview_data_url'); return { previewDataUrl: json.preview_data_url, previewCount: json.preview_count ?? 1, progressSource: 'complete' } }
export async function getDraftResult(draftId: string): Promise<{ resultDataUrl: string; mediaType: 'image' | 'video' }> { const json = await rawJson<{ result_data_url?: string; media_type?: 'image' | 'video' }>(`/admin/draft/${encodeURIComponent(draftId)}/result`); if (!json.result_data_url) throw new Error('result 响应缺少 result_data_url'); return { resultDataUrl: json.result_data_url, mediaType: json.media_type ?? 'image' } }
export async function deleteSessionDrafts(sessionId: string): Promise<{ session_id: string; deleted_count: number }> { return rawJson(`/admin/drafts/session/${encodeURIComponent(sessionId)}`, { method: 'DELETE' }) }
export type ConfirmDraftResult = { videoId: string; status: string; mediaType: 'video' } | { upscaledUrl: string; targetResolution: [number, number]; algorithm: string; mediaType: 'image' | 'video' }
export async function confirmDraft(draftId: string): Promise<ConfirmDraftResult> { const json = await rawJson<{ media_type?: 'image' | 'video'; video_id?: string; status?: string; upscaled_url?: string; target_resolution?: [number, number]; algorithm?: string }>(`/admin/draft/${encodeURIComponent(draftId)}/confirm`, { method: 'POST' }); if (json.media_type === 'video' && json.video_id) return { videoId: json.video_id, status: json.status ?? 'generating', mediaType: 'video' }; if (!json.upscaled_url) throw new Error('confirm 响应缺少 upscaled_url'); return { upscaledUrl: json.upscaled_url, targetResolution: json.target_resolution ?? [0, 0], algorithm: json.algorithm ?? 'comfyui', mediaType: json.media_type ?? 'image' } }
export async function rejectDraft(draftId: string): Promise<{ newDraftId: string; previewUrl: string; attemptNumber: number; maxAttempts: number }> { const json = await rawJson<{ new_draft_id?: string; preview_url?: string; attempt_number?: number; max_attempts?: number }>(`/admin/draft/${encodeURIComponent(draftId)}/reject`, { method: 'POST' }); if (!json.new_draft_id || !json.preview_url) throw new Error('reject 响应缺少 new_draft_id / preview_url'); return { newDraftId: json.new_draft_id, previewUrl: json.preview_url, attemptNumber: json.attempt_number ?? 1, maxAttempts: json.max_attempts ?? 5 } }
export async function getDraftStatus(draftId: string): Promise<{ status: string; expiresAt: number; attemptNumber: number; maxAttempts: number; progress: number; stage: string; workflowVersion: string; progressSource?: string }> { const json = await rawJson<{ status?: string; expires_at?: number; attempt_number?: number; max_attempts?: number; progress?: number; stage?: string; workflow_version?: string; progress_source?: string }>(`/admin/draft/${encodeURIComponent(draftId)}`); return { status: json.status ?? 'unknown', expiresAt: json.expires_at ?? 0, attemptNumber: json.attempt_number ?? 1, maxAttempts: json.max_attempts ?? 5, progress: json.progress ?? 0, stage: json.stage ?? json.status ?? 'unknown', workflowVersion: json.workflow_version ?? '', progressSource: json.progress_source } }
export async function getVideoStatus(videoId: string): Promise<VideoStatusResponse> { return rawJson<VideoStatusResponse>(`/admin/console/videos/${encodeURIComponent(videoId)}`) }

export async function listModels(): Promise<ApiResponse<ModelListData>> { return fetchJson<ModelListData>('/v1/models') }
export async function createEmbeddings(body: EmbeddingRequest): Promise<ApiResponse<EmbeddingListData>> { return fetchJson<EmbeddingListData>('/v1/embeddings', { method: 'POST', body: JSON.stringify(body) }) }
export async function listApiKeys(page = 1, pageSize = 20): Promise<ApiResponse<ApiKeyListData>> { return fetchJson<ApiKeyListData>(`/admin/api-keys?page=${page}&page_size=${pageSize}`) }
export async function createApiKey(body: CreateApiKeyRequest): Promise<ApiResponse<CreateApiKeyData>> { return fetchJson<CreateApiKeyData>('/admin/api-keys', { method: 'POST', body: JSON.stringify(body) }) }
export async function deleteApiKey(keyId: string): Promise<ApiResponse<RevokedKeyData>> { return fetchJson<RevokedKeyData>(`/admin/api-keys/${encodeURIComponent(keyId)}`, { method: 'DELETE' }) }
export async function rotateApiKey(keyId: string): Promise<{ data: { key: string; warning: string } }> { return fetchJson<{ key: string; warning: string }>(`/admin/api-keys/${encodeURIComponent(keyId)}/rotate`, { method: 'POST', body: JSON.stringify({}) }) }
export interface UpdateQuotaRequest { daily_tokens?: number; monthly_cost?: number; rate_limit_rpm?: number; rate_limit_tpm?: number }
export interface UpdateQuotaData { id: string; user_id: string; quotas: { daily_tokens_limit: number; monthly_cost_limit: number; rate_limit_rpm: number; rate_limit_tpm: number } }
export async function updateApiKeyQuota(keyId: string, body: UpdateQuotaRequest): Promise<ApiResponse<UpdateQuotaData>> { return fetchJson<UpdateQuotaData>(`/admin/api-keys/${encodeURIComponent(keyId)}`, { method: 'PUT', body: JSON.stringify(body) }) }
export async function getQuota(keyId: string): Promise<ApiResponse<DetailedQuotaData>> { return fetchJson<DetailedQuotaData>(`/admin/quotas/${encodeURIComponent(keyId)}`) }
export async function listGroups(): Promise<ApiResponse<GroupListData>> { return fetchJson<GroupListData>('/admin/groups') }
export async function createGroup(body: CreateGroupRequest): Promise<ApiResponse<Group>> { return fetchJson<Group>('/admin/groups', { method: 'POST', body: JSON.stringify(body) }) }
export async function updateGroup(groupId: string, body: UpdateGroupRequest): Promise<ApiResponse<Group>> { return fetchJson<Group>(`/admin/groups/${encodeURIComponent(groupId)}`, { method: 'PUT', body: JSON.stringify(body) }) }
export async function deleteGroup(groupId: string): Promise<ApiResponse<{ deleted: boolean }>> { return fetchJson<{ deleted: boolean }>(`/admin/groups/${encodeURIComponent(groupId)}`, { method: 'DELETE' }) }
export async function assignKeyGroup(keyId: string, groupId: string, cacheScope?: CacheScope): Promise<ApiResponse<unknown>> { return fetchJson<unknown>(`/admin/api-keys/${encodeURIComponent(keyId)}/group`, { method: 'PUT', body: JSON.stringify({ group_id: groupId, cache_scope: cacheScope }) }) }

export async function getHealth(): Promise<ApiResponse<HealthData>> { const res = await fetch(`${API_BASE}/health`, { credentials: 'include' }); if (!res.ok) throw new Error(`Health check failed: ${res.status}`); return res.json() }
export async function getMetricsText(): Promise<string> { const res = await fetch(`${API_BASE}/metrics`, { credentials: 'include' }); if (!res.ok) throw new Error(`Failed to fetch metrics: ${res.status}`); return res.text() }
export function parseMetrics(text: string): MetricSample[] { const samples: MetricSample[] = []; for (const line of text.split('\n')) { if (!line.startsWith('gateway_') || line.startsWith('#')) continue; const match = line.match(/^(.+?)\{(.+?)\} (.+)$/); if (match) { const [, name, labelsStr, value] = match; const labels: Record<string, string> = {}; for (const pair of labelsStr.split(',')) { const [k, v] = pair.split('=').map(s => s.replace(/"/g, '')); if (k && v !== undefined) labels[k] = v } samples.push({ name, labels, value: parseFloat(value) }) } else { const simpleMatch = line.match(/^(.+?) (.+)$/); if (simpleMatch) { const [, name, value] = simpleMatch; samples.push({ name, labels: {}, value: parseFloat(value) }) } } } return samples }
export interface MetricsQueryResponse { status: string; data: { resultType: string; result: Array<{ metric?: Record<string, string>; values?: Array<{ timestamp: string; value: string }> }> } }
export async function metricsQuery(params: { query: string; start?: string; end?: string; step?: string }): Promise<MetricsQueryResponse> { const qs = new URLSearchParams({ query: params.query, step: params.step || '3600' }); if (params.start) qs.set('start', params.start); if (params.end) qs.set('end', params.end); const res = await fetch(`${API_BASE}/admin/metrics/query_range?${qs}`, { credentials: 'include' }); if (!res.ok) throw new Error(`Failed to query metrics: ${res.status}`); return res.json() }
export interface MetricsJsonData { prometheus: Record<string, { labels: Record<string, string>; value: number }>; prometheus_series?: Record<string, Array<{ labels: Record<string, string>; value: number }>>; keys: { total_keys: number; total_daily_tokens_used: number; total_monthly_cost_used: number; total_requests: number }; circuit_breakers: Record<string, unknown>; uptime_seconds: number }
export async function getMetricsJson(): Promise<ApiResponse<MetricsJsonData>> { return fetchJson<MetricsJsonData>('/admin/metrics-json') }
export interface LedgerRow { id: number; trace_id: string; ts: string; ts_unix: number; user_id: string; group_id: string; model: string; provider: string; pipeline_kind: string; tokens_in: number; tokens_out: number; tokens_total: number; tokens_saved: number; cost_usd: number; duration_ms: number; cached: number; stream: number; status: string }
interface AggregateRow { k: string; requests: number; tokens_in: number; tokens_out: number; tokens_total: number; tokens_saved: number; cost_usd: number; avg_latency_ms: number; cache_hits: number }
interface AggregateDayRow { k: string; requests: number; tokens_total: number; cost_usd: number }
interface LatencyHourRow { k: string; samples: number; avg_latency_ms: number }
export interface CostSummary { total: Record<string, number>; by_model: AggregateRow[]; by_user: AggregateRow[]; by_group: AggregateRow[]; by_day: AggregateDayRow[]; latency_by_hour: LatencyHourRow[] }
export async function getCostLedger(params?: { limit?: number; offset?: number; start?: number | null; end?: number | null; user_id?: string | null; group_id?: string | null; model?: string | null }): Promise<LedgerRow[]> { const qs = new URLSearchParams(); if (params?.limit) qs.set('limit', String(params.limit)); if (params?.offset) qs.set('offset', String(params.offset)); if (params?.start !== undefined && params.start !== null) qs.set('start', String(params.start)); if (params?.end !== undefined && params.end !== null) qs.set('end', String(params.end)); if (params?.user_id) qs.set('user_id', params.user_id); if (params?.group_id) qs.set('group_id', params.group_id); if (params?.model) qs.set('model', params.model); const body = await rawJson<{ rows?: LedgerRow[] }>(`/admin/costs/ledger${qs.toString() ? '?' + qs : ''}`); return body.rows ?? [] }
export async function getCostSummary(days?: number): Promise<CostSummary> { return rawJson<CostSummary>(`/admin/costs/summary${days ? `?days=${days}` : ''}`) }

export interface PluginConfigItem { name: string; enabled: boolean; depends_on: string[]; config: Record<string, unknown>; pipeline_kind?: 'understanding' | 'generation'; priority?: number; debug?: boolean | null }
export interface PluginsConfigData { plugins: PluginConfigItem[] }
export interface GlobalConfigData { hot_reload: boolean; debug_mode: boolean }
export interface DebugConfig { frontend?: boolean; entry?: boolean; cache?: boolean; bridge?: boolean; plugins_enabled?: boolean; [key: string]: unknown }
export async function getPluginsConfig(): Promise<ApiResponse<PluginsConfigData>> { return fetchJson<PluginsConfigData>('/admin/plugins-config') }
export async function togglePlugin(name: string, enabled: boolean): Promise<ApiResponse<{ name: string; enabled: boolean }>> { return fetchJson<{ name: string; enabled: boolean }>('/admin/plugins-config', { method: 'PUT', body: JSON.stringify({ name, enabled }) }) }
export async function getGlobalConfig(): Promise<ApiResponse<GlobalConfigData>> { return fetchJson<GlobalConfigData>('/admin/global-config') }
export async function updateGlobalConfig(config: { hot_reload: boolean; debug_mode?: boolean }): Promise<ApiResponse<GlobalConfigData>> { return fetchJson<GlobalConfigData>('/admin/global-config', { method: 'PUT', body: JSON.stringify(config) }) }
export async function getFullConfig(): Promise<ApiResponse<Record<string, unknown>>> { return fetchJson<Record<string, unknown>>('/admin/config') }
export async function updateFullConfig(config: Record<string, unknown>): Promise<ApiResponse<{ updated: boolean }>> { return fetchJson<{ updated: boolean }>('/admin/config', { method: 'PUT', body: JSON.stringify(config) }) }
export interface ComfyUIStatus {
  available: boolean
  manager_enabled: boolean
  public_url: string
  manager_url: string
  gpu: Record<string, unknown> | null
  queue: { running: number; pending: number } | null
  disk: { total_bytes: number; free_bytes: number } | null
  error: string | null
}
export interface GenerationPreset {
  id: string
  name: string
  kind: 'image' | 'video' | 'upscale'
  builtin: boolean
  enabled: boolean
  languages: string[]
  validation: { missing_models: string[]; missing_nodes: string[] }
}
export async function getComfyUIStatus(): Promise<ApiResponse<ComfyUIStatus>> { return fetchJson<ComfyUIStatus>('/admin/comfyui/status') }
export async function getGenerationPresets(): Promise<ApiResponse<GenerationPreset[]>> { return fetchJson<GenerationPreset[]>('/admin/generation-presets') }
export async function setPluginDebug(name: string, enabled: boolean): Promise<ApiResponse<{ plugin: string; debug: boolean }>> { return fetchJson<{ plugin: string; debug: boolean }>(`/admin/plugins/${encodeURIComponent(name)}/debug`, { method: 'POST', body: JSON.stringify({ enabled }) }) }
export async function getDebugConfig(): Promise<DebugConfig> { return (await fetchJson<DebugConfig>('/admin/config/debug')).data }
export async function updateDebugSection(config: Partial<DebugConfig>): Promise<ApiResponse<GlobalConfigData>> {
  const current = await getDebugConfig()
  const merged = { ...current, ...config }
  const plugins = {
    enabled: Boolean(merged.plugins_enabled),
    per_plugin: (
      typeof merged.per_plugin === 'object' && merged.per_plugin !== null
        ? merged.per_plugin
        : {}
    ),
  }
  const debug = {
    frontend: Boolean(merged.frontend),
    entry: Boolean(merged.entry),
    cache: Boolean(merged.cache),
    bridge: Boolean(merged.bridge),
    plugins,
  }
  return fetchJson<GlobalConfigData>('/admin/global-config', { method: 'PUT', body: JSON.stringify({ debug }) })
}
export interface ProviderConnectivityResult { success: boolean; latency_ms: number; error?: string | null }
export async function testProviderConnectivity(provider: string, config?: Record<string, unknown>): Promise<ApiResponse<ProviderConnectivityResult>> { return fetchJson<ProviderConnectivityResult>(`/admin/providers/${encodeURIComponent(provider)}/test`, { method: 'POST', body: JSON.stringify(config ?? {}) }) }
export async function fetchProviderModels(provider: string): Promise<ApiResponse<{ models: string[] }>> { return fetchJson<{ models: string[] }>(`/admin/providers/${encodeURIComponent(provider)}/models`) }

export interface PluginTraceStep { plugin_name: string; duration_ms: number; status: 'success' | 'skipped' | 'failed' }
export interface LogEntry { request_id: string; trace_id: string; user_id: string; timestamp: number; method: string; endpoint: string; model: string; status: number; duration_ms: number; cache_hit: boolean; tier: string | null; plugin_trace?: PluginTraceStep[] }
export interface TraceEvent { trace_id: string; ts: number; stage: string; kind: string; name: string; duration_ms: number; status: string; payload?: Record<string, unknown> | null }
export interface TraceDetail { trace_id: string; request_id: string; user_id: string; model: string; endpoint: string; status: number; duration_ms: number; cache_hit: boolean; cache_tier: string | null; timestamp: number; events: TraceEvent[]; plugin_trace: PluginTraceStep[]; related_requests: LogEntry[]; meta?: { wall_start?: number } | null }
export interface LogsData { items: LogEntry[]; pagination: { page: number; pageSize: number; total: number } }
export async function getRequestLogs(params: { page?: number; pageSize?: number; user_id?: string; model?: string; status?: string; cache_only?: boolean }): Promise<ApiResponse<LogsData>> { const qs = new URLSearchParams(); if (params.page) qs.set('page', String(params.page)); if (params.pageSize) qs.set('page_size', String(params.pageSize)); if (params.user_id) qs.set('user_id', params.user_id); if (params.model) qs.set('model', params.model); if (params.status) qs.set('status', params.status); if (params.cache_only !== undefined) qs.set('cache_only', String(params.cache_only)); return fetchJson<LogsData>(`/admin/logs?${qs}`) }
export async function deleteAllLogs(): Promise<ApiResponse<{ deleted: boolean }>> { return fetchJson<{ deleted: boolean }>('/admin/logs', { method: 'DELETE' }) }
export async function batchDeleteLogs(requestIds: string[]): Promise<ApiResponse<{ deleted: number; requested: number }>> { return fetchJson<{ deleted: number; requested: number }>('/admin/logs/batch-delete', { method: 'POST', body: JSON.stringify({ request_ids: requestIds }) }) }
export async function getTraceDetail(traceId: string): Promise<ApiResponse<TraceDetail>> { return fetchJson<TraceDetail>(`/admin/trace/${encodeURIComponent(traceId)}`) }

export interface RuntimeCapability { installed: boolean; configured: boolean; available: boolean; reason?: string | null; install_command: string | null; commands?: string[]; details?: Record<string, unknown> }
export interface RuntimeCapabilitiesData { profile: string; capabilities: Record<string, RuntimeCapability> }
export async function getRuntimeCapabilities(): Promise<ApiResponse<RuntimeCapabilitiesData>> { return fetchJson<RuntimeCapabilitiesData>('/admin/capabilities') }
export interface RagDocument { doc_id: string; filename: string; file_type: string; chunk_count: number; chunk_strategy: string; chunk_size: number; chunk_overlap: number; total_tokens: number; created_at: number; url: string }
export async function listRagDocuments(): Promise<ApiResponse<{ documents: RagDocument[] }>> { return fetchJson<{ documents: RagDocument[] }>('/admin/rag/documents') }
export async function importRagDocument(params: { url?: string; content?: string; filename?: string; chunk_strategy?: string; chunk_size?: number; chunk_overlap?: number }): Promise<ApiResponse<{ doc_id: string; filename: string; chunk_count: number; total_tokens: number; elapsed_ms: number }>> { return fetchJson('/admin/rag/documents', { method: 'POST', body: JSON.stringify(params) }) }
export async function deleteRagDocument(docId: string): Promise<ApiResponse<{ deleted: boolean }>> { return fetchJson<{ deleted: boolean }>(`/admin/rag/documents/${encodeURIComponent(docId)}`, { method: 'DELETE' }) }

export type CodeImportSourceType = 'folder' | 'server_path' | 'git' | 'zip'
export type CodeImportTaskStatus = 'pending' | 'scanning' | 'splitting' | 'building_graph' | 'embedding' | 'completed' | 'failed' | 'cancelled'
export interface CodeImportTask { task_id: string; status: CodeImportTaskStatus; current_file: string | null; done: number; total: number; error: string | null; source_label: string | null; source_type: string | null; created_at: number }
export interface CodeRepositoryImport { document_id: string; source_type: CodeImportSourceType; source_label: string; file_count: number; language_summary: string[]; function_count: number; class_count: number; chunk_count: number; embedding_model: string; import_time: string }
export type CodeImportJsonPayload = { source_type: 'server_path'; server_path: string; embedding_model: string } | { source_type: 'git'; git_url: string; git_branch?: string; embedding_model: string }
export async function importCodeRepository(payload: FormData | CodeImportJsonPayload): Promise<{ task_id: string; status: 'pending' }> { const headers = await ensureAuthHeaders(); const init: RequestInit = payload instanceof FormData ? { method: 'POST', credentials: 'include', body: payload } : { method: 'POST', credentials: 'include', headers, body: JSON.stringify(payload) }; const res = await fetch(`${API_BASE}/admin/rag/code/import`, init); if (!res.ok) throw new Error(await errorText(res, 'Code import failed')); return res.json() }
export async function listCodeImportTasks(): Promise<CodeImportTask[]> { return rawJson<CodeImportTask[]>('/admin/rag/code/tasks') }
export async function getCodeImportTask(taskId: string): Promise<CodeImportTask> { return rawJson<CodeImportTask>(`/admin/rag/code/tasks/${encodeURIComponent(taskId)}`) }
export async function cancelCodeImportTask(taskId: string): Promise<{ task_id: string; status: CodeImportTaskStatus }> { return rawJson(`/admin/rag/code/tasks/${encodeURIComponent(taskId)}/cancel`, { method: 'POST' }) }
export async function listCodeRepositories(): Promise<CodeRepositoryImport[]> { return rawJson<CodeRepositoryImport[]>('/admin/rag/code/repositories') }
export async function deleteCodeRepository(documentId: string): Promise<void> { await rawJson(`/admin/rag/code/repositories/${encodeURIComponent(documentId)}`, { method: 'DELETE' }) }
export interface CodeFileSyncResult { document_id: string; synced_files: number; refreshed_symbols: number; deleted_files?: number }
export async function syncCodeRepository(documentId: string): Promise<CodeFileSyncResult> { return rawJson<CodeFileSyncResult>(`/admin/rag/code/repositories/${encodeURIComponent(documentId)}/sync`, { method: 'POST' }) }
export interface CodeSymbolNode { id: string | null; kind: string | null; name: string | null; qualified_name: string | null; file_path: string | null; language: string | null; start_line: number | null; end_line: number | null; signature: string | null; docstring: string | null }
export interface CodeSymbolRef { name: string | null; kind: string | null; file_path: string | null; start_line: number | null }
export interface CodeFile { path: string; language: string; node_count: number | null; size: number | null }
export async function queryCodeSymbols(documentId: string, symbol: string, opts?: { kind?: string; limit?: number }): Promise<CodeSymbolNode[]> { const params = new URLSearchParams({ symbol, limit: String(opts?.limit ?? 10) }); if (opts?.kind) params.set('kind', opts.kind); return rawJson<CodeSymbolNode[]>(`/admin/rag/code/repositories/${encodeURIComponent(documentId)}/query?${params}`) }
export async function listCodeFiles(documentId: string): Promise<CodeFile[]> { return rawJson<CodeFile[]>(`/admin/rag/code/repositories/${encodeURIComponent(documentId)}/files`) }
export async function listAllSymbols(documentId: string, opts?: { kind?: string; limit?: number }): Promise<CodeSymbolNode[]> { const params = new URLSearchParams({ symbol: '', limit: String(opts?.limit ?? 5000) }); if (opts?.kind) params.set('kind', opts.kind); return rawJson<CodeSymbolNode[]>(`/admin/rag/code/repositories/${encodeURIComponent(documentId)}/query?${params}`) }
export async function getCodeCallers(documentId: string, symbol: string): Promise<CodeSymbolRef[]> { const body = await rawJson<{ callers?: CodeSymbolRef[] }>(`/admin/rag/code/repositories/${encodeURIComponent(documentId)}/callers?${new URLSearchParams({ symbol })}`); return body.callers ?? [] }
export async function getCodeCallees(documentId: string, symbol: string): Promise<CodeSymbolRef[]> { const body = await rawJson<{ callees?: CodeSymbolRef[] }>(`/admin/rag/code/repositories/${encodeURIComponent(documentId)}/callees?${new URLSearchParams({ symbol })}`); return body.callees ?? [] }
export async function getCodeImpact(documentId: string, symbol: string, depth = 2): Promise<CodeSymbolRef[]> { const params = new URLSearchParams({ symbol, depth: String(depth) }); const body = await rawJson<{ affected?: CodeSymbolRef[] }>(`/admin/rag/code/repositories/${encodeURIComponent(documentId)}/impact?${params}`); return body.affected ?? [] }

export interface L3CacheConfig { default_mode: 'auto' | 'manual'; auto_cleanup_interval_minutes: number; default_ttl_hours: number; min_ttl_hours: number; max_ttl_hours: number }
export interface L3CacheEntry { id: string; promptPreview: string; model: string; userId: string; createdAt: number; expiresAt: number | null; mode: 'auto' | 'manual'; hitCount: number; tokenCount: number }
export interface L3EntriesData { items: L3CacheEntry[]; pagination: { page: number; pageSize: number; total: number } }
export async function getL3CacheConfig(): Promise<ApiResponse<L3CacheConfig>> { return fetchJson<L3CacheConfig>('/admin/cache/l3/config') }
export async function updateL3CacheConfig(config: Partial<L3CacheConfig>): Promise<ApiResponse<L3CacheConfig>> { return fetchJson<L3CacheConfig>('/admin/cache/l3/config', { method: 'PUT', body: JSON.stringify(config) }) }
export async function listL3Entries(params: { page?: number; pageSize?: number; mode?: string; userId?: string; sortBy?: string }): Promise<ApiResponse<L3EntriesData>> { const qs = new URLSearchParams(); if (params.page) qs.set('page', String(params.page)); if (params.pageSize) qs.set('page_size', String(params.pageSize)); if (params.mode) qs.set('mode', params.mode); if (params.userId) qs.set('user_id', params.userId); if (params.sortBy) qs.set('sort_by', params.sortBy); return fetchJson<L3EntriesData>(`/admin/cache/l3/entries?${qs}`) }
export async function updateL3EntryMode(pointId: string, mode: 'auto' | 'manual', ttlHours?: number): Promise<ApiResponse<L3CacheEntry>> { return fetchJson<L3CacheEntry>(`/admin/cache/l3/entries/${encodeURIComponent(pointId)}/mode`, { method: 'PUT', body: JSON.stringify({ mode, ttl_hours: ttlHours }) }) }
export async function deleteL3Entry(pointId: string): Promise<ApiResponse<{ deleted: boolean }>> { return fetchJson<{ deleted: boolean }>(`/admin/cache/l3/entries/${encodeURIComponent(pointId)}`, { method: 'DELETE' }) }
export async function triggerL3Cleanup(): Promise<ApiResponse<{ deleted_count: number }>> { return fetchJson<{ deleted_count: number }>('/admin/cache/l3/cleanup', { method: 'POST' }) }
