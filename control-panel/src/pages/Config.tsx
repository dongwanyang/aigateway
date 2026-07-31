import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Save, RefreshCw, AlertTriangle, ExternalLink } from 'lucide-react'
import Card from '@/components/Card'
import { getComfyUIStatus, getGenerationPresets } from '@/api/client'
import { queryKeys } from '@/query/keys'

type ConfigValue = string | number | boolean | null | ConfigValue[] | { [key: string]: ConfigValue }
type ConfigObject = Record<string, ConfigValue>
type PanelResponse<T> = { data: T; message: string; revision?: string }

interface VersionedConfig {
  config: ConfigObject
  revision: string
}

interface ConfigSchemaItem {
  path: string
  module: string
  description: string
}

interface ConfigRow {
  path: string
  group: string
  value: ConfigValue
  description: string
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
  return {
    config: toConfigValue(response.data) as ConfigObject,
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

function isPlainObject(value: unknown): value is Record<string, ConfigValue> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.filter((item): item is string => typeof item === 'string' && item.length > 0)
}

function toConfigValue(value: unknown): ConfigValue {
  if (value === null || typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return value
  if (Array.isArray(value)) return value.map(toConfigValue)
  if (isPlainObject(value)) {
    const out: Record<string, ConfigValue> = {}
    for (const [key, child] of Object.entries(value)) out[key] = toConfigValue(child)
    return out
  }
  return String(value)
}

function valueToText(value: ConfigValue): string {
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (value === null) return 'null'
  return JSON.stringify(value, null, 2)
}

function parseEditedValue(input: string, previous: ConfigValue): ConfigValue {
  if (typeof previous === 'boolean') return input === 'true'
  if (typeof previous === 'number') {
    if (input.trim() === '') throw new Error('数字不能为空')
    const parsed = Number(input)
    if (!Number.isFinite(parsed)) throw new Error('数字格式无效')
    return parsed
  }
  if (previous === null) {
    if (input.trim() === '' || input.trim() === 'null') return null
    try { return toConfigValue(JSON.parse(input)) } catch { return input }
  }
  if (Array.isArray(previous) || isPlainObject(previous)) {
    try { return toConfigValue(JSON.parse(input)) } catch { throw new Error('JSON 格式无效') }
  }
  return input
}

function normalizePath(path: string): string {
  return path.replace(/\[\d+\]/g, '[]')
}

function descriptionForPath(path: string, value: ConfigValue, schema: Map<string, string>): string {
  const normalized = normalizePath(path)
  const description = schema.get(normalized) ?? schema.get(path)
  if (description) return description
  if (Array.isArray(value)) return '配置模板未提供说明；该值为列表，可用 JSON 数组格式编辑。'
  if (isPlainObject(value)) return '配置模板未提供说明；该值为对象，可用 JSON 对象格式编辑。'
  return '配置模板未提供说明。请在 config.yaml.template 中为该参数补充行内注释。'
}

function flattenConfig(value: ConfigValue, schema: Map<string, string>, path: string[] = []): ConfigRow[] {
  const currentPath = path.join('.')
  const group = path[0] ?? 'root'
  if (Array.isArray(value)) {
    if (value.length === 0 || value.every(item => !isPlainObject(item) && !Array.isArray(item))) {
      return [{ path: currentPath, group, value, description: descriptionForPath(currentPath, value, schema) }]
    }
    return value.flatMap((item, index) => flattenConfig(item, schema, [...path.slice(0, -1), `${path.at(-1)}[${index}]`]))
  }
  if (isPlainObject(value)) {
    const entries = Object.entries(value)
    if (entries.length === 0) return [{ path: currentPath, group, value, description: descriptionForPath(currentPath, value, schema) }]
    return entries.flatMap(([key, child]) => flattenConfig(child, schema, [...path, key]))
  }
  return [{ path: currentPath, group, value, description: descriptionForPath(currentPath, value, schema) }]
}

function parsePath(path: string): Array<string | number> {
  const parts: Array<string | number> = []
  for (const raw of path.split('.')) {
    const match = raw.match(/^([^\[]+)(?:\[(\d+)\])?$/)
    if (!match) { parts.push(raw); continue }
    parts.push(match[1])
    if (match[2] !== undefined) parts.push(Number(match[2]))
  }
  return parts
}

function readByPath(root: ConfigValue, path: string): ConfigValue {
  let cursor: ConfigValue = root
  for (const part of parsePath(path)) {
    if (typeof part === 'number') {
      cursor = Array.isArray(cursor) ? cursor[part] : null
    } else {
      cursor = isPlainObject(cursor) ? cursor[part] : null
    }
  }
  return cursor
}

function writeByPath(root: ConfigValue, path: string, value: ConfigValue): ConfigValue {
  const cloned = structuredClone(root)
  let cursor: ConfigValue = cloned
  const parts = parsePath(path)
  for (let index = 0; index < parts.length - 1; index += 1) {
    const part = parts[index]
    if (typeof part === 'number') {
      if (!Array.isArray(cursor)) throw new Error(`路径无效: ${path}`)
      cursor = cursor[part]
    } else {
      if (!isPlainObject(cursor)) throw new Error(`路径无效: ${path}`)
      cursor = cursor[part]
    }
  }
  const last = parts.at(-1)
  if (typeof last === 'number') {
    if (!Array.isArray(cursor)) throw new Error(`路径无效: ${path}`)
    cursor[last] = value
  } else if (last) {
    if (!isPlainObject(cursor)) throw new Error(`路径无效: ${path}`)
    cursor[last] = value
  }
  return cloned
}

function groupRows(rows: ConfigRow[]): Array<[string, ConfigRow[]]> {
  const groups = new Map<string, ConfigRow[]>()
  for (const row of rows) {
    const list = groups.get(row.group) ?? []
    list.push(row)
    groups.set(row.group, list)
  }
  return Array.from(groups.entries())
}

function ConfigValueEditor({ row, onChange }: { row: ConfigRow; onChange: (path: string, input: string) => boolean }) {
  const canonicalText = valueToText(row.value)
  const [draftText, setDraftText] = useState(canonicalText)
  const [editing, setEditing] = useState(false)

  useEffect(() => {
    if (!editing) setDraftText(canonicalText)
  }, [canonicalText, editing, row.path])

  function updateDraft(next: string) {
    setDraftText(next)
    if (Array.isArray(row.value) || isPlainObject(row.value)) {
      try {
        JSON.parse(next)
      } catch {
        return
      }
      onChange(row.path, next)
      return
    }
    if (typeof row.value === 'number') {
      if (next.trim() === '' || !Number.isFinite(Number(next))) return
    }
    onChange(row.path, next)
  }

  function commitDraft() {
    setEditing(false)
    if (draftText === canonicalText) return
    if (!onChange(row.path, draftText)) setDraftText(canonicalText)
  }

  if (typeof row.value === 'boolean') {
    return (
      <select
        value={String(row.value)}
        onChange={event => onChange(row.path, event.target.value)}
        className="input"
        style={{ width: '100%', fontSize: '12px' }}
      >
        <option value="true">true</option>
        <option value="false">false</option>
      </select>
    )
  }
  if (Array.isArray(row.value) || isPlainObject(row.value)) {
    return (
      <textarea
        value={draftText}
        onFocus={() => setEditing(true)}
        onChange={event => updateDraft(event.target.value)}
        onBlur={commitDraft}
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
        if (event.key === 'Escape') {
          setDraftText(canonicalText)
          event.currentTarget.blur()
        }
      }}
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
  const saveMutation = useMutation({ mutationFn: updateTableConfig })
  const config = configQuery.data?.config ?? null
  const revision = configQuery.data?.revision ?? ''
  const loading = configQuery.isLoading || schemaQuery.isLoading
  const saving = saveMutation.isPending
  const remoteError = configQuery.error ?? schemaQuery.error ?? saveMutation.error
  const error = localError ?? (remoteError instanceof Error ? remoteError.message : null)
  const comfyStatus = comfyQuery.data as ComfyStatusView | undefined
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
    const map = new Map<string, string>()
    for (const item of schemaQuery.data ?? []) {
      if (item.path && item.description) map.set(item.path, item.description)
    }
    return map
  }, [schemaQuery.data])
  const rows = useMemo(() => draftConfig ? flattenConfig(draftConfig, schemaMap) : [], [draftConfig, schemaMap])
  const groupedRows = useMemo(() => groupRows(rows), [rows])

  async function loadConfig() {
    setLocalError(null)
    setSuccess(null)
    setHasChanges(false)
    await Promise.all([
      configQuery.refetch(),
      schemaQuery.refetch(),
      comfyQuery.refetch(),
      presetsQuery.refetch(),
    ])
  }

  async function handleSave() {
    setLocalError(null)
    setSuccess(null)
    if (!draftConfig) return
    try {
      const result = await saveMutation.mutateAsync({
        config: draftConfig as Record<string, unknown>,
        revision,
      })
      queryClient.setQueryData<VersionedConfig>(queryKeys.config.full, {
        config: structuredClone(draftConfig),
        revision: result.revision ?? revision,
      })
      setSuccess('配置已保存并生效')
      setHasChanges(false)
      setTimeout(() => setSuccess(null), 3000)
    } catch (exc) {
      setLocalError(exc instanceof Error ? exc.message : '保存失败')
    }
  }

  function handleValueChange(path: string, input: string): boolean {
    if (!draftConfig) return false
    setLocalError(null)
    try {
      const previous = readByPath(draftConfig, path)
      const parsed = parseEditedValue(input, previous)
      const next = writeByPath(draftConfig, path, parsed) as ConfigObject
      setDraftConfig(next)
      setHasChanges(JSON.stringify(next) !== JSON.stringify(config))
      return true
    } catch (exc) {
      setLocalError(exc instanceof Error ? `${path}: ${exc.message}` : '配置值格式无效')
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
          <button className="btn btn-primary" style={{ padding: '8px 14px', fontSize: '12px' }} onClick={handleSave} disabled={saving || !hasChanges || Boolean(localError)}>
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
        </div>

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
                          <td style={{ padding: '8px', verticalAlign: 'top' }}><ConfigValueEditor row={row} onChange={handleValueChange} /></td>
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
