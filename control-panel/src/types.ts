/**
 * TypeScript 类型定义 — 与 API_CONTRACT.md 完全对齐
 */

// ------------------------------------------------------------------
// 统一响应格式
// ------------------------------------------------------------------

export interface ApiResponse<T = unknown> {
  data: T
  message: string
}

export interface ApiError {
  error: {
    code: string
    message: string
  }
}

// ------------------------------------------------------------------
// 错误码
// ------------------------------------------------------------------

export type ErrorCode =
  | 'validation_error'
  | 'pii_rejected'
  | 'invalid_model'
  | 'unauthorized'
  | 'forbidden'
  | 'quota_exceeded_daily_tokens'
  | 'quota_exceeded_monthly_cost'
  | 'quota_exceeded_group_daily_tokens'
  | 'quota_exceeded_group_monthly_cost'
  | 'rate_limit_rpm'
  | 'rate_limit_tpm'
  | 'rate_limit_group_rpm'
  | 'rate_limit_group_tpm'
  | 'circuit_breaker_open'
  | 'upstream_timeout'
  | 'internal_error'
  | 'not_found'
  | 'conflict'
  | 'service_unavailable'

// ------------------------------------------------------------------
// 聊天补全
// ------------------------------------------------------------------

export interface ChatMessage {
  role: 'system' | 'user' | 'assistant' | 'tool'
  content: string
  name?: string
  tool_call_id?: string
}

export interface ToolDefinition {
  type: 'function'
  function: {
    name: string
    description?: string
    parameters: Record<string, unknown>
  }
}

export interface ChatCompletionRequest {
  model: string
  messages: ChatMessage[]
  temperature?: number
  max_tokens?: number | null
  top_p?: number
  frequency_penalty?: number
  presence_penalty?: number
  stream?: boolean
  tools?: ToolDefinition[]
  tool_choice?: string | { type: 'function'; function: { name: string } }
  stop?: string | string[]
  user?: string
}

export interface ChatChoice {
  index: number
  finish_reason: 'stop' | 'length' | 'tool_calls' | 'content_filter' | null
  message: {
    role: 'assistant'
    content: string | null
    tool_calls?: ChatToolCall[]
  }
  delta?: {
    role?: 'assistant'
    content?: string | null
    tool_calls?: ChatToolCallDelta[]
  }
}

export interface ChatToolCall {
  id: string
  type: 'function'
  function: {
    name: string
    arguments: string
  }
}

export interface ChatToolCallDelta {
  index: number
  id?: string
  type?: 'function'
  function?: {
    name?: string
    arguments?: string
  }
}

export interface ChatUsage {
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
}

export interface ChatRoutedTo {
  provider: string
  model: string
  fallback_chain: string[]
}

export interface PluginTrace {
  plugin_name: string
  duration_ms: number
  status: 'success' | 'skipped' | 'failed'
}

export interface ChatMeta {
  cache_hit: boolean
  cache_tier: 'L1' | 'L2' | 'L3' | null
  plugin_trace: PluginTrace[]
  routed_to: ChatRoutedTo | null
}

export interface ChatCompletionData {
  id: string
  object: 'chat.completion'
  created: number
  model: string
  choices: ChatChoice[]
  usage?: ChatUsage
  _meta?: ChatMeta
}

export interface ChatCompletionChunkData {
  id: string
  object: 'chat.completion.chunk'
  created: number
  model: string
  choices: {
    index: number
    delta: {
      role?: 'assistant'
      content?: string | null
      tool_calls?: ChatToolCallDelta[]
    }
    finish_reason: 'stop' | 'length' | 'tool_calls' | 'content_filter' | null
  }[]
  usage?: ChatUsage
}

// ------------------------------------------------------------------
// 模型列表
// ------------------------------------------------------------------

export interface ModelPermission {
  id: string
  object: 'model_permission'
  created: number
  allow_create_engine: boolean
  allow_sampling: boolean
  allow_logprobs: boolean
  allow_search_indices: boolean
  allow_view: boolean
  allow_fine_tuning: boolean
  organization: string
  group: string | null
  is_blocking: boolean
}

export interface ModelInfo {
  id: string
  object: 'model'
  created: number
  owned_by: string
  permission: ModelPermission[]
}

export interface ModelListData {
  object: 'list'
  data: ModelInfo[]
}

// ------------------------------------------------------------------
// Embeddings
// ------------------------------------------------------------------

export interface EmbeddingData {
  object: 'embedding'
  index: number
  embedding: number[]
}

export interface EmbeddingUsage {
  prompt_tokens: number
  total_tokens: number
}

export interface EmbeddingListData {
  object: 'list'
  data: EmbeddingData[]
  usage: EmbeddingUsage
}

export interface EmbeddingRequest {
  model: string
  input: string | string[]
  user?: string
}

// ------------------------------------------------------------------
// API Keys & Quotas
// ------------------------------------------------------------------

export interface ApiKeyQuotas {
  daily_tokens_used: number
  daily_tokens_limit: number
  monthly_cost_used: number
  monthly_cost_limit: number
  rpm_current: number
  rpm_limit: number
  tpm_current: number
  tpm_limit: number
}

export interface ApiKeyUsagePercentage {
  daily_tokens: number
  monthly_cost: number
}

export interface ApiKeyItem {
  id: string
  key_prefix: string
  user_id: string
  group_id: string
  group_name?: string
  cache_scope: CacheScope
  created_at: string
  last_used_at: string | null
  status: 'active' | 'revoked' | 'suspended'
  quotas: ApiKeyQuotas
  usage_percentage: ApiKeyUsagePercentage
}

export interface PaginationInfo {
  page: number
  pageSize: number
  total: number
}

export interface ApiKeyListData {
  items: ApiKeyItem[]
  pagination: PaginationInfo
}

export interface CreateApiKeyRequest {
  user_id: string
  group_id?: string
  cache_scope?: CacheScope
  daily_tokens?: number
  monthly_cost?: number
  rate_limit_rpm?: number
  rate_limit_tpm?: number
}

export interface CreateApiKeyData {
  id: string
  key: string
  key_prefix: string
  user_id: string
  created_at: string
  status: string
  quotas: {
    daily_tokens: number
    monthly_cost: number
    rate_limit_rpm: number
    rate_limit_tpm: number
  }
}

export interface RevokedKeyData {
  id: string
  status: 'revoked'
  revoked_at: string
}

export interface QuotaAlert {
  type: 'budget_warning' | 'rate_limit_warning'
  threshold_percent: number
  message: string
}

export interface DetailedQuotaData {
  id: string
  user_id: string
  status: 'active' | 'revoked' | 'suspended'
  quotas: {
    daily_tokens: { used: number; limit: number; reset_at: string }
    monthly_cost: { used: number; limit: number; reset_at: string }
    rate_limit: {
      rpm: { current: number; limit: number }
      tpm: { current: number; limit: number }
    }
  }
  alerts: QuotaAlert[]
  last_request_at: string | null
  total_requests_today: number
  total_tokens_today: number
}

// ------------------------------------------------------------------
// 健康检查
// ------------------------------------------------------------------

export interface DependencyStatus {
  status: 'connected' | 'disconnected' | 'error'
  latency_ms: number
}

export interface PluginStatus {
  enabled: boolean
  status: 'healthy' | 'degraded' | 'error'
}

export interface HealthData {
  status: 'healthy' | 'degraded' | 'unhealthy'
  version: string
  uptime_seconds: number
  timestamp: string
  dependencies: Record<string, DependencyStatus>
  plugins: Record<string, PluginStatus>
}

// ------------------------------------------------------------------
// 熔断器状态
// ------------------------------------------------------------------

export type CircuitState = 'CLOSED' | 'OPEN' | 'HALF_OPEN'

export interface CircuitBreakerStatus {
  provider: string
  state: CircuitState
  state_value: number
  failure_count: number
  failure_threshold: number
  last_failure_time: number
  last_success_time: number
}

// ------------------------------------------------------------------
// 插件配置
// ------------------------------------------------------------------

export interface PluginConfig {
  name: string
  enabled: boolean
  depends_on: string[]
  config: Record<string, unknown>
}

// ------------------------------------------------------------------
// Prometheus 指标解析类型
// ------------------------------------------------------------------

export interface MetricSample {
  name: string
  labels: Record<string, string>
  value: number
}

// ------------------------------------------------------------------
// Cache Scope
// ------------------------------------------------------------------

export type CacheScope = 'private' | 'group' | 'public'

// ------------------------------------------------------------------
// User Groups
// ------------------------------------------------------------------

export interface GroupQuotas {
  daily_tokens_limit: number
  daily_tokens_used: number
  monthly_cost_limit: number
  monthly_cost_used: number
  rate_limit_rpm: number
  rate_limit_tpm: number
}

export interface Group {
  group_id: string
  name: string
  status: 'active' | 'suspended'
  created_at: string
  updated_at: string
  member_count: number
  daily_tokens_limit: number
  daily_tokens_used: number
  monthly_cost_limit: number
  monthly_cost_used: number
  rate_limit_rpm: number
  rate_limit_tpm: number
}

export interface GroupListData {
  items: Group[]
  total: number
}

export interface CreateGroupRequest {
  name: string
  daily_tokens?: number
  monthly_cost?: number
  rate_limit_rpm?: number
  rate_limit_tpm?: number
}

export interface UpdateGroupRequest {
  daily_tokens?: number
  monthly_cost?: number
  rate_limit_rpm?: number
  rate_limit_tpm?: number
  status?: 'active' | 'suspended'
}

export interface AssignGroupRequest {
  group_id: string
  cache_scope?: CacheScope
}

// ------------------------------------------------------------------
// Chat 页面本地类型(聊天窗 MVP)
// ------------------------------------------------------------------

/** 聊天页单条消息(区别于 OpenAI wire 类型 ChatMessage) */
export interface ChatPageMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  intent?: 'understanding' | 'generation:image' | 'generation:video' | null
  model?: string
  error?: boolean
  ts: number
}

/** GET /v1/videos/{id} 返回的上游视频任务状态(passthrough) */
export interface VideoStatusResponse {
  id?: string
  status?: string  // 'queued' | 'in_progress' | 'succeeded' | 'failed' | ...
  video?: { url?: string }
  error?: { code?: string; message?: string }
}
