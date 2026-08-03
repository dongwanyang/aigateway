import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Save, RefreshCw, AlertTriangle, ExternalLink } from 'lucide-react'
import Card from '@/components/Card'
import { getComfyUIStatus, getGenerationPresets, getGpuStatus, releaseGpuMemory } from '@/api/client'
import { queryKeys } from '@/query/keys'
import {
  type ConfigObject,
  type ConfigRow,
  type ConfigSchemaItem,
  flattenConfig,
  groupRows,
  isCompactStringArray,
  isPlainObject,
  parseEditedValue,
  readByPath,
  toConfigValue,
  valueToText,
  writeByPath,
} from './configEditor'

type PanelResponse<T> = { data: T; message: string; revision?: string }

interface VersionedConfig {
  config: ConfigObject
  revision: string
}

interface ComfyStatusView {
  available?: boolean
  public_url?: string
  manager_url?: string
  queue?: { running?: number; pending?: number } | null
  configuration_status?: string
  configuration_errors?: unknown
  error?: string | null
}

interface GatewayGpuStatusView {
  available?: boolean
  torch_initialized?: boolean
  cuda_disabled?: boolean
  allocated_bytes?: number
  reserved_bytes?: number
  error?: string | null
}

interface GpuExecutionView {
  available?: boolean
  mode?: 'scheduler_pool' | 'gateway_pool' | 'scheduler_error' | 'gateway' | 'delegated_comfyui' | 'unavailable'
  topology_complete?: boolean
  runnable_now?: boolean
  device_count?: number
  worker_count?: number
  runnable_worker_count?: number
  error?: string | null
}

interface GenerationPresetView {
  configuration_status?: 'ready' | 'disabled' | 'configuration_error'
  configuration_errors?: unknown
}

const API_BASE = import.meta.env.VITE_API_BASE ?? ''

const GROUP_LABELS: Record<string, string> = {
  server: '服务器配置',
  plugin_runtime: '插件运行时',
  retry_budget: '重试预算',
  intent_classifier: '意图识别',
  model_selector: '模型选择',
  task_routing: '任务路由',
  generation: '生成接口',
  generation_optimization: '生成优化',
  gpu_scheduler: 'GPU 动态资源池',
  auth: '认证与配额',
  plugins: '理解管道插件',
  providers: '模型提供商',
  embedding: '向量嵌入',
  observability: '可观测性',
  infrastructure: '基础设施',
  cache: '缓存',
  circuit_breaker: '熔断器',
  rate_limiter: '速率限制',
  streaming: '流式响应',
  code_rag: 'Code RAG',
  media_optimization: '媒体优化',
  debug: '调试开关',
  hot_reload: '热重载',
  debug_mode: '调试模式',
}

function normalizeRevision(value: unknown): string | null {
  if (typeof value !== 'string' || !value.trim()) return null
  return value.trim().replace(/^W\//, '').replace(/^"|"$/g, '') || null
}

async function fetchPanelJson<T>(path: string, options: RequestInit = {}): Promise<PanelResponse<T>> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(options.headers ?? {}) },
  })
  const body = await res.json().catch(() => ({}))
  if (!res.ok) {
    const code = body?.error?.code ?? body?.detail?.error?.code
    const message = code === 'config_version_conflict'
      ? '配置已被其他会话修改，请重新加载后再保存。'
      : body?.error?.message ?? body?.detail?.error?.message ?? body?.detail ?? `HTTP ${res.status}`
    throw new Error(String(message))
  }
  const revision = normalizeRevision(body?.revision ?? res.headers.get('etag'))
  return { ...body, ...(revision ? { revision } : {}) } as PanelResponse<T>
}

async function getVersionedConfig(): Promise<VersionedConfig> {
  const response = await fetchPanelJson<ConfigObject>('/admin/config')
  const config = toConfigValue(response.data) as ConfigObject
  const gpuScheduler = config.gpu_scheduler
  if (
    isPlainObject(gpuScheduler)
    && !('comfyui_dynamic_vram_enabled' in gpuScheduler)
  ) {
    gpuScheduler.comfyui_dynamic_vram_enabled = false
  }
  return {
    config,
    revision: response.revision ?? '',
  }
}

async function getConfigSchema(): Promise<PanelResponse<{ items: ConfigSchemaItem[] }>> {
  return fetchPanelJson<{ items: ConfigSchemaItem[] }>('/admin/config/schema')
}

async function updateTableConfig(input: { config: Record<string, unknown>; revision: string }): Promise<PanelResponse<{ updated: boolean }>> {
  return fetchPanelJson<{ updated: boolean }>('/admin/config/table', {
    method: 'PUT',
    headers: input.revision ? { 'If-Match': `"${input.revision}"` } : {},
    body: JSON.stringify(input.config),
  })
}

function formatBytes(value: unknown): string {
  const bytes = typeof value === 'number' && Number.isFinite(value) ? value : 0
  if (bytes <= 0) return '0 B'
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1)
  return `${(bytes / 1024 ** index).toFixed(index >= 3 ? 2 : 0)} ${units[index]}`
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.filter((item): item is string => typeof item === 'string' && item.length > 0)
}

export function ConfigValueEditor({
  row,
  onChange,
  onValidityChange,
}: {
  row: ConfigRow
  onChange: (row: ConfigRow, input: string) => boolean
  onValidityChange: (path: string, valid: boolean) => void
}) {
  const isGpuDeviceSelector = (
    row.path === 'gpu_scheduler.gateway_devices'
    || row.path === 'gpu_scheduler.comfyui_devices'
  )
  const canonicalText = isGpuDeviceSelector && Array.isArray(row.value)
    ? row.value.join(', ')
    : valueToText(row.value, row.schemaType, row.schemaEditor)
  const compactStringArray = isCompactStringArray(
    row.value,
    row.schemaType,
    row.schemaEditor,
  )
  const [draftText, setDraftText] = useState(canonicalText)
  const [editing, setEditing] = useState(false)
  const [draftInvalid, setDraftInvalid] = useState(false)

  useEffect(() => {
    if (!editing && !draftInvalid) setDraftText(canonicalText)
  }, [canonicalText, draftInvalid, editing, row.path])

  function applyValidDraft(next: string) {
    const accepted = onChange(row, next)
    setDraftInvalid(!accepted)
    onValidityChange(row.path, accepted)
  }

  function updateDraft(next: string) {
    setDraftText(next)
    if (compactStringArray) {
      try {
        parseEditedValue(next, row.value, row.schemaType, row.schemaEditor)
      } catch {
        setDraftInvalid(true)
        onValidityChange(row.path, false)
        return
      }
      applyValidDraft(next)
      return
    }
    if (Array.isArray(row.value) || isPlainObject(row.value)) {
      try {
        JSON.parse(next)
      } catch {
        setDraftInvalid(true)
        onValidityChange(row.path, false)
        return
      }
      applyValidDraft(next)
      return
    }
    if (typeof row.value === 'number') {
      if (next.trim() === '' || !Number.isFinite(Number(next))) {
        setDraftInvalid(true)
        onValidityChange(row.path, false)
        return
      }
    }
    applyValidDraft(next)
  }

  function commitDraft() {
    if (draftText === canonicalText) {
      setDraftInvalid(false)
      setEditing(false)
      onValidityChange(row.path, true)
      return
    }
    const accepted = onChange(row, draftText)
    setDraftInvalid(!accepted)
    setEditing(false)
    onValidityChange(row.path, accepted)
  }

  if (typeof row.value === 'boolean') {
    return (
      <select
        value={String(row.value)}
        onChange={event => onChange(row, event.target.value)}
        className="input"
        style={{ width: '100%', fontSize: '12px' }}
      >
        <option value="true">true</option>
        <option value="false">false</option>
      </select>
    )
  }
  if (row.path === 'gpu_scheduler.policy' || row.path === 'gpu_scheduler.gateway_fallback') {
    const options = row.path.endsWith('policy')
      ? [['auto', '自动调度'], ['manual', '手工设备池']]
      : [['cpu', '回退 CPU'], ['wait', '等待 GPU'], ['fail', '立即失败']]
    return (
      <select
        className="input"
        value={String(row.value)}
        onChange={event => onChange(row, event.target.value)}
        style={{ width: '100%', fontSize: '12px' }}
      >
        {options.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
      </select>
    )
  }
  if (isGpuDeviceSelector) {
    return (
      <input
        className="input"
        type="text"
        value={draftText}
        onFocus={() => setEditing(true)}
        onChange={event => setDraftText(event.target.value)}
        onBlur={commitDraft}
        placeholder="auto，或逗号分隔 GPU UUID"
        style={{ width: '100%', fontFamily: 'var(--font-mono)', fontSize: '12px' }}
      />
    )
  }
  if (compactStringArray) {
    return (
      <input
        className="input"
        type="text"
        value={draftText}
        onFocus={() => setEditing(true)}
        onChange={event => updateDraft(event.target.value)}
        onBlur={commitDraft}
        onKeyDown={event => {
          if (event.key === 'Enter') event.currentTarget.blur()
        }}
        aria-invalid={draftInvalid}
        placeholder="逗号分隔，如 tool_calling, structured_output"
        style={{ width: '100%', fontFamily: 'var(--font-mono)', fontSize: '12px' }}
      />
    )
  }
  if (Array.isArray(row.value) || isPlainObject(row.value)) {
    return (
      <textarea
        value={draftText}
        onFocus={() => setEditing(true)}
        onChange={event => updateDraft(event.target.value)}
        onBlur={commitDraft}
        aria-invalid={draftInvalid}
        style={{ width: '100%', minHeight: 76, fontFamily: 'var(--font-mono)', fontSize: '12px' }}
        spellCheck={false}
      />
    )
  }
  return (
    <input
      className="input"
      type={typeof row.value === 'number' ? 'number' : 'text'}
      value={draftText}
      onFocus={() => setEditing(true)}
      onChange={event => updateDraft(event.target.value)}
      onBlur={commitDraft}
      onKeyDown={event => {
        if (event.key === 'Enter') event.currentTarget.blur()
      }}
      aria-invalid={draftInvalid}
      style={{ width: '100%', fontFamily: 'var(--font-mono)', fontSize: '12px' }}
    />
  )
}

export default function Config() {
  const queryClient = useQueryClient()
  const [draftConfig, setDraftConfig] = useState<ConfigObject | null>(null)
  const [localError, setLocalError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [hasChanges, setHasChanges] = useState(false)
  const [invalidDraftPaths, setInvalidDraftPaths] = useState<Set<string>>(() => new Set())
  const [editorEpoch, setEditorEpoch] = useState(0)
  const configQuery = useQuery({
    queryKey: queryKeys.config.full,
    queryFn: getVersionedConfig,
  })
  const schemaQuery = useQuery({
    queryKey: ['config-schema'],
    queryFn: async () => {
      const response = await getConfigSchema()
      return Array.isArray(response.data?.items) ? response.data.items : []
    },
  })
  const comfyQuery = useQuery({
    queryKey: ['comfyui', 'status'],
    queryFn: async () => (await getComfyUIStatus()).data,
    refetchInterval: 30_000,
  })
  const presetsQuery = useQuery({
    queryKey: ['generation-presets'],
    queryFn: async () => (await getGenerationPresets()).data,
  })
  const gpuQuery = useQuery({
    queryKey: ['gpu', 'status'],
    queryFn: async () => (await getGpuStatus()).data,
    refetchInterval: 30_000,
  })
  const releaseGpuMutation = useMutation({
    mutationFn: releaseGpuMemory,
    onSuccess: async () => {
      await Promise.all([gpuQuery.refetch(), comfyQuery.refetch()])
    },
  })
  const saveMutation = useMutation({ mutationFn: updateTableConfig })
  const config = configQuery.data?.config ?? null
  const revision = configQuery.data?.revision ?? ''
  const loading = configQuery.isLoading || schemaQuery.isLoading
  const saving = saveMutation.isPending
  const remoteError = configQuery.error ?? schemaQuery.error ?? saveMutation.error
  const error = localError ?? (remoteError instanceof Error ? remoteError.message : null)
  const comfyStatus = comfyQuery.data as ComfyStatusView | undefined
  const gatewayStatus = gpuQuery.data?.gateway as GatewayGpuStatusView | undefined
  const gpuExecution = gpuQuery.data?.execution as GpuExecutionView | undefined
  const comfyConfigurationErrors = stringList(comfyStatus?.configuration_errors)
  const comfyHasConfigurationError = comfyConfigurationErrors.length > 0
  const comfyStatusText = comfyQuery.isLoading
    ? '检测中'
    : comfyHasConfigurationError
      ? 'ComfyUI 配置错误'
      : comfyStatus?.available
        ? 'ComfyUI 可用'
        : 'ComfyUI 服务不可达'
  const comfyStatusColor = comfyQuery.isLoading
    ? 'var(--color-text-tertiary)'
    : comfyHasConfigurationError
      ? 'var(--color-warning)'
      : comfyStatus?.available
        ? 'var(--color-success)'
        : 'var(--color-danger)'

  useEffect(() => {
    if (config && !hasChanges) setDraftConfig(structuredClone(config))
  }, [config, hasChanges])

  const schemaMap = useMemo(() => {
    const map = new Map<string, ConfigSchemaItem>()
    for (const item of schemaQuery.data ?? []) {
      if (item.path && item.description) map.set(item.path, item)
    }
    return map
  }, [schemaQuery.data])
  const rows = useMemo(() => draftConfig ? flattenConfig(draftConfig, schemaMap) : [], [draftConfig, schemaMap])
  const groupedRows = useMemo(() => groupRows(rows), [rows])

  function handleDraftValidity(path: string, valid: boolean) {
    setInvalidDraftPaths(previous => {
      const next = new Set(previous)
      if (valid) next.delete(path)
      else next.add(path)
      return next
    })
  }

  async function loadConfig() {
    setLocalError(null)
    setSuccess(null)
    setHasChanges(false)
    setInvalidDraftPaths(new Set())
    setEditorEpoch(previous => previous + 1)
    await Promise.all([
      configQuery.refetch(),
      schemaQuery.refetch(),
      comfyQuery.refetch(),
      presetsQuery.refetch(),
      gpuQuery.refetch(),
    ])
  }

  async function handleSave() {
    setLocalError(null)
    setSuccess(null)
    if (!draftConfig || invalidDraftPaths.size > 0) return
    try {
      const restartRequired = config != null && [
        'gateway_devices',
        'comfyui_devices',
        'device_overrides',
        'comfyui_dynamic_vram_enabled',
      ].some(key => JSON.stringify(
        (config.gpu_scheduler as ConfigObject | undefined)?.[key],
      ) !== JSON.stringify(
        (draftConfig.gpu_scheduler as ConfigObject | undefined)?.[key],
      ))
      const result = await saveMutation.mutateAsync({
        config: draftConfig as Record<string, unknown>,
        revision,
      })
      queryClient.setQueryData<VersionedConfig>(queryKeys.config.full, {
        config: structuredClone(draftConfig),
        revision: result.revision ?? revision,
      })
      const topologyAutoApply = (
        draftConfig.gpu_scheduler as ConfigObject | undefined
      )?.topology_auto_apply === true
      setSuccess(restartRequired
        ? topologyAutoApply
          ? '配置已保存；已允许外部拓扑控制器自动应用（需管理员单独运行 --watch）'
          : '配置已保存；请重新运行 quickstart 或手工运行 GPU 拓扑控制器'
        : '配置已保存并热生效（已取得的租约保持原期限）')
      setHasChanges(false)
      setInvalidDraftPaths(new Set())
      setTimeout(() => setSuccess(null), 3000)
    } catch (exc) {
      setLocalError(exc instanceof Error ? exc.message : '保存失败')
    }
  }

  function handleValueChange(row: ConfigRow, input: string): boolean {
    if (!draftConfig) return false
    setLocalError(null)
    try {
      const previous = readByPath(draftConfig, row.segments)
      const parsed = (
        row.path === 'gpu_scheduler.gateway_devices'
        || row.path === 'gpu_scheduler.comfyui_devices'
      )
        ? input.trim() === 'auto'
          ? 'auto'
          : input.split(',').map(item => item.trim()).filter(Boolean)
        : parseEditedValue(
            input,
            previous,
            row.schemaType,
            row.schemaEditor,
          )
      const next = writeByPath(draftConfig, row.segments, parsed) as ConfigObject
      setDraftConfig(next)
      setHasChanges(JSON.stringify(next) !== JSON.stringify(config))
      return true
    } catch (exc) {
      setLocalError(exc instanceof Error ? `${row.path}: ${exc.message}` : '配置值格式无效')
      return false
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-bold">系统配置</h2>
        <div className="flex items-center gap-2">
          <button className="btn btn-secondary" style={{ padding: '8px 14px', fontSize: '12px' }} onClick={loadConfig} disabled={loading}>
            <RefreshCw size={14} /> 重新加载
          </button>
          <button className="btn btn-primary" style={{ padding: '8px 14px', fontSize: '12px' }} onClick={handleSave} disabled={saving || !hasChanges || invalidDraftPaths.size > 0 || Boolean(localError)}>
            <Save size={14} /> {saving ? '保存中...' : '保存配置'}
          </button>
        </div>
      </div>

      {hasChanges && (
        <div style={{ padding: '10px 16px', borderRadius: '8px', backgroundColor: 'rgba(245, 158, 11, 0.1)', border: '1px solid var(--color-warning)', fontSize: '13px', color: 'var(--color-warning)', display: 'flex', alignItems: 'center', gap: '8px' }}>
          <AlertTriangle size={14} />
          配置已修改但未保存。点击“保存配置”使变更生效。
        </div>
      )}

      {error && (
        <div style={{ padding: '10px 16px', borderRadius: '8px', backgroundColor: 'rgba(239, 68, 68, 0.1)', border: '1px solid var(--color-danger)', fontSize: '13px', color: 'var(--color-danger)' }}>
          ❌ {error}
        </div>
      )}

      {success && (
        <div style={{ padding: '10px 16px', borderRadius: '8px', backgroundColor: 'rgba(16, 185, 129, 0.1)', border: '1px solid var(--color-success)', fontSize: '13px', color: 'var(--color-success)' }}>
          ✅ {success}
        </div>
      )}

      <Card>
        <div className="flex items-center justify-between mb-3">
          <div>
            <h3 className="font-semibold">本地生成</h3>
            <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
              Gateway 提供简易入口；节点、模型和高级工作流仍在 ComfyUI Manager 中管理。
            </p>
          </div>
          <span style={{ color: comfyStatusColor }}>{comfyStatusText}</span>
        </div>

        {comfyHasConfigurationError && (
          <div style={{ marginBottom: '16px', padding: '12px 14px', borderRadius: '8px', border: '1px solid var(--color-warning)', backgroundColor: 'rgba(245, 158, 11, 0.08)', color: 'var(--color-warning)' }}>
            <div className="flex items-center gap-2 font-semibold text-sm">
              <AlertTriangle size={15} />
              本地生成配置不完整
            </div>
            <div className="text-xs mt-2" style={{ fontFamily: 'var(--font-mono)', lineHeight: 1.7 }}>
              {comfyConfigurationErrors.map(item => <div key={item}>{item}</div>)}
            </div>
          </div>
        )}

        <div className="flex flex-wrap gap-2 mb-4">
          {comfyStatus?.public_url ? (
            <a className="btn btn-secondary" href={comfyStatus.public_url} target="_blank" rel="noreferrer">
              <ExternalLink size={14} /> 打开 ComfyUI
            </a>
          ) : (
            <button className="btn btn-secondary" disabled>未配置 ComfyUI 地址</button>
          )}
          {comfyStatus?.manager_url ? (
            <a className="btn btn-secondary" href={comfyStatus.manager_url} target="_blank" rel="noreferrer">
              <ExternalLink size={14} /> 打开 Manager
            </a>
          ) : (
            <button className="btn btn-secondary" disabled>未配置 Manager 地址</button>
          )}
          {comfyStatus?.queue && (
            <span className="text-sm">队列：{comfyStatus.queue.running ?? 0} 运行 / {comfyStatus.queue.pending ?? 0} 等待</span>
          )}
          <button
            className="btn btn-secondary"
            disabled={!gpuQuery.data || gpuQuery.data.queue_idle === false || releaseGpuMutation.isPending}
            onClick={() => releaseGpuMutation.mutate()}
            title={gpuQuery.data?.queue_idle === false ? '队列非空时不能释放显存' : '卸载空闲模型并清理 PyTorch 缓存'}
          >
            <RefreshCw size={14} /> {releaseGpuMutation.isPending ? '释放中...' : '释放空闲显存'}
          </button>
        </div>

        {gpuQuery.data && (
          <div className="grid grid-cols-1 gap-3 mb-4 md:grid-cols-2">
            <div className="rounded-lg border p-3" style={{ borderColor: 'var(--color-border)' }}>
              <div className="text-xs font-semibold mb-2">Gateway / PyTorch</div>
              {gpuExecution?.mode === 'scheduler_error' ? (
                <div role="alert" className="space-y-1">
                  <div className="text-xs font-medium" style={{ color: 'var(--color-danger)' }}>GPU 资源池拓扑错误</div>
                  <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                    Scheduler 已启用，但本地设备与 ComfyUI Worker 未形成有效 UUID 配对
                    {gpuExecution.error ? `：${gpuExecution.error}` : '。'}
                  </div>
                </div>
              ) : gpuExecution?.mode === 'scheduler_pool' && gpuExecution.available ? (
                <div role="status" className="space-y-1">
                  <div className="text-xs font-medium" style={{ color: 'var(--color-success)' }}>
                    动态资源池可用 · {gpuExecution.device_count ?? gpuQuery.data.scheduler?.devices?.length ?? 0} 张 GPU
                  </div>
                  <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                    Gateway 空闲时可借用 GPU；生成排队后停止新租约并等待在途推理安全结束。
                  </div>
                </div>
              ) : gpuExecution?.mode === 'scheduler_pool' ? (
                <div role="alert" className="space-y-1">
                  <div className="text-xs font-medium" style={{ color: 'var(--color-warning)' }}>GPU 资源池暂不可调度</div>
                  <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                    拓扑已建立，但当前没有健康且未处于冷却或 OOM 隔离的 Worker
                    {gpuExecution.error ? `：${gpuExecution.error}` : '。'}
                  </div>
                </div>
              ) : gpuExecution?.mode === 'gateway_pool' ? (
                <div role="status" className="space-y-1">
                  <div className="text-xs font-medium" style={{ color: gpuExecution.available ? 'var(--color-success)' : 'var(--color-warning)' }}>
                    Gateway GPU 池{gpuExecution.available ? '可用' : '暂不可用'}
                  </div>
                  <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                    本地 GPU 由 Scheduler 管理；外部 ComfyUI 不参与本机设备租约。
                  </div>
                </div>
              ) : gpuExecution?.mode === 'delegated_comfyui' ? (
                <div role="status" className="space-y-1">
                  <div className="text-xs font-medium" style={{ color: 'var(--color-success)' }}>使用外部 ComfyUI</div>
                  <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                    本地 GPU Scheduler 已禁用，生成任务直接提交到外部端点。
                  </div>
                </div>
              ) : gatewayStatus?.torch_initialized ? (
                <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                  allocated {formatBytes(gatewayStatus.allocated_bytes)} · reserved {formatBytes(gatewayStatus.reserved_bytes)}
                </div>
              ) : gpuExecution?.mode === 'gateway' || gatewayStatus?.available ? (
                <div role="status" className="space-y-1">
                  <div className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>未初始化 CUDA</div>
                  <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                    GPU 设备可用，但 Gateway 尚未建立 CUDA 上下文。
                  </div>
                </div>
              ) : (
                <div role="alert" className="space-y-1">
                  <div className="text-xs font-medium" style={{ color: 'var(--color-danger)' }}>GPU 状态不可用</div>
                  <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                    无法获取有效 GPU 执行状态{gpuExecution?.error || gatewayStatus?.error ? `：${gpuExecution?.error ?? gatewayStatus?.error}` : '。'}
                  </div>
                </div>
              )}
            </div>
            <div className="rounded-lg border p-3" style={{ borderColor: 'var(--color-border)' }}>
              <div className="text-xs font-semibold mb-2">ComfyUI / Device</div>
              <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                used {formatBytes(gpuQuery.data.comfyui.memory?.used_bytes)} · free {formatBytes(gpuQuery.data.comfyui.memory?.free_bytes)}
              </div>
            </div>
            <div className="md:col-span-2 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
              队列为空只表示没有执行任务；模型权重和 PyTorch reserved cache 仍可能常驻显存。
              {gpuQuery.data.shared_gpu ? ' 当前 Gateway 与 ComfyUI 共用同一块 GPU。' : ''}
            </div>
          </div>
        )}

        {(gpuQuery.data?.scheduler?.devices?.length ?? 0) > 0 && (
          <div className="mb-4 overflow-x-auto">
            <div className="flex items-center justify-between mb-2">
              <div className="text-xs font-semibold">设备池与 Worker</div>
              <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                模式 {gpuQuery.data?.scheduler?.policy} · 生成队列 {gpuQuery.data?.scheduler?.generation_queue_depth ?? 0}
              </div>
            </div>
            <table style={{ width: '100%', minWidth: 820, fontSize: 12, borderCollapse: 'collapse' }}>
              <thead>
                <tr style={{ color: 'var(--color-text-tertiary)', textAlign: 'left' }}>
                  <th style={{ padding: 8 }}>逻辑卡 / UUID</th>
                  <th style={{ padding: 8 }}>显存</th>
                  <th style={{ padding: 8 }}>状态</th>
                  <th style={{ padding: 8 }}>Gateway</th>
                  <th style={{ padding: 8 }}>ComfyUI Worker / 队列</th>
                  <th style={{ padding: 8 }}>冷却</th>
                </tr>
              </thead>
              <tbody>
                {gpuQuery.data?.scheduler?.devices.map(device => (
                  <tr key={device.uuid} style={{ borderTop: '1px solid var(--color-border)' }}>
                    <td style={{ padding: 8 }}>
                      <div>{device.logical_index} · {device.name}</div>
                      <div style={{ color: 'var(--color-text-tertiary)', fontFamily: 'var(--font-mono)' }}>{device.uuid}</div>
                    </td>
                    <td style={{ padding: 8 }}>{device.free_memory_gb.toFixed(1)} / {device.total_memory_gb.toFixed(1)} GB 可用</td>
                    <td style={{ padding: 8 }}>{device.state}</td>
                    <td style={{ padding: 8 }}>{device.gateway_leases} 租约 · {device.resident_components.join(', ') || '无驻留模型'}</td>
                    <td style={{ padding: 8 }}>{device.worker_id ?? '未分配'} · {device.queue.running} 运行 / {device.queue.pending} 等待</td>
                    <td style={{ padding: 8 }}>{Math.ceil(Math.max(device.cooldown_remaining_seconds, device.oom_quarantine_remaining_seconds))} 秒</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {releaseGpuMutation.error instanceof Error && (
          <div role="alert" className="text-xs mb-4" style={{ color: 'var(--color-danger)' }}>
            显存释放失败：{releaseGpuMutation.error.message}
          </div>
        )}

        <div className="space-y-2">
          {(Array.isArray(presetsQuery.data) ? presetsQuery.data : []).map(preset => {
            const presetView = preset as typeof preset & GenerationPresetView
            const configurationErrors = stringList(presetView.configuration_errors)
            const dependencyMissing = [
              ...stringList(preset.validation?.missing_models),
              ...stringList(preset.validation?.missing_nodes),
            ].filter(item => !configurationErrors.includes(item))
            const issueText = configurationErrors.length
              ? `配置错误：${configurationErrors.join('、')}`
              : dependencyMissing.length
                ? `缺少依赖：${dependencyMissing.join('、')}`
                : presetView.configuration_status === 'disabled'
                  ? '已禁用'
                  : '依赖完整'
            const issueColor = configurationErrors.length || dependencyMissing.length
              ? 'var(--color-warning)'
              : presetView.configuration_status === 'disabled'
                ? 'var(--color-text-tertiary)'
                : 'var(--color-success)'
            return (
              <div key={preset.id} className="flex items-start justify-between gap-3 text-sm">
                <span>{preset.name} <small>({preset.kind})</small></span>
                <span style={{ color: issueColor, textAlign: 'right', overflowWrap: 'anywhere' }}>
                  {issueText}
                </span>
              </div>
            )
          })}
        </div>
      </Card>

      <Card>
        <div className="flex items-center justify-between mb-3">
          <div>
            <h3 className="font-semibold">配置参数</h3>
            <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
              按功能模块分组展示 config.yaml。第三列说明来自 config.yaml.template 行内注释；敏感配置已脱敏显示。
            </p>
          </div>
          <span className="text-xs" style={{ color: 'var(--color-text-quaternary)' }}>{rows.length} 个参数</span>
        </div>

        {loading || !draftConfig ? (
          <div className="space-y-3">{[1, 2, 3, 4, 5].map(i => <div key={i} className="h-4 skeleton rounded" />)}</div>
        ) : (
          <div className="space-y-6">
            {groupedRows.map(([group, groupItems]) => (
              <div key={group}>
                <h4 className="font-semibold mb-2">{GROUP_LABELS[group] ?? group}</h4>
                <div style={{ overflowX: 'auto' }}>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '13px' }}>
                    <thead>
                      <tr style={{ color: 'var(--color-text-tertiary)', textAlign: 'left', borderBottom: '1px solid var(--color-border)' }}>
                        <th style={{ width: '30%', padding: '8px' }}>参数名</th>
                        <th style={{ width: '35%', padding: '8px' }}>参数值</th>
                        <th style={{ width: '35%', padding: '8px' }}>参数介绍</th>
                      </tr>
                    </thead>
                    <tbody>
                      {groupItems.map(row => (
                        <tr key={row.path} style={{ borderBottom: '1px solid var(--color-border-subtle)' }}>
                          <td style={{ padding: '8px', fontFamily: 'var(--font-mono)', verticalAlign: 'top', wordBreak: 'break-all' }}>{row.path}</td>
                          <td style={{ padding: '8px', verticalAlign: 'top' }}>
                            <ConfigValueEditor
                              key={`${editorEpoch}:${row.path}`}
                              row={row}
                              onChange={handleValueChange}
                              onValidityChange={handleDraftValidity}
                            />
                          </td>
                          <td style={{ padding: '8px', color: 'var(--color-text-tertiary)', verticalAlign: 'top' }}>{row.description}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  )
}
