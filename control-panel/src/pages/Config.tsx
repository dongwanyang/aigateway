import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Save, RefreshCw, AlertTriangle, ExternalLink } from 'lucide-react'
import Card from '@/components/Card'
import { getComfyUIStatus, getFullConfig, getGenerationPresets } from '@/api/client'
import { queryKeys } from '@/query/keys'

type ConfigValue = string | number | boolean | null | ConfigValue[] | { [key: string]: ConfigValue }
type ConfigObject = Record<string, ConfigValue>

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

async function fetchPanelJson<T>(path: string, options: RequestInit = {}): Promise<{ data: T; message: string }> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(options.headers ?? {}) },
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    const message = body?.error?.message ?? body?.detail?.error?.message ?? body?.detail ?? `HTTP ${res.status}`
    throw new Error(String(message))
  }
  return res.json()
}

async function getConfigSchema(): Promise<{ data: { items: ConfigSchemaItem[] }; message: string }> {
  return fetchPanelJson<{ items: ConfigSchemaItem[] }>('/admin/config/schema')
}

async function updateTableConfig(config: Record<string, unknown>): Promise<{ data: { updated: boolean }; message: string }> {
  return fetchPanelJson<{ updated: boolean }>('/admin/config/table', { method: 'PUT', body: JSON.stringify(config) })
}

function isPlainObject(value: unknown): value is Record<string, ConfigValue> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
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

function ConfigValueEditor({ row, onChange }: { row: ConfigRow; onChange: (path: string, input: string) => void }) {
  const text = valueToText(row.value)
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
        value={text}
        onChange={event => onChange(row.path, event.target.value)}
        style={{ width: '100%', minHeight: 76, fontFamily: 'var(--font-mono)', fontSize: '12px' }}
        spellCheck={false}
      />
    )
  }
  return (
    <input
      className="input"
      type={typeof row.value === 'number' ? 'number' : 'text'}
      value={text}
      onChange={event => onChange(row.path, event.target.value)}
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
    queryFn: async () => toConfigValue((await getFullConfig()).data) as ConfigObject,
  })
  const schemaQuery = useQuery({
    queryKey: ['config-schema'],
    queryFn: async () => (await getConfigSchema()).data.items,
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
  const config = configQuery.data ?? null
  const loading = configQuery.isLoading || schemaQuery.isLoading
  const saving = saveMutation.isPending
  const remoteError = configQuery.error ?? schemaQuery.error ?? saveMutation.error
  const error = localError ?? (remoteError instanceof Error ? remoteError.message : null)

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
    await Promise.all([configQuery.refetch(), schemaQuery.refetch()])
  }

  async function handleSave() {
    setLocalError(null)
    setSuccess(null)
    if (!draftConfig) return
    try {
      await saveMutation.mutateAsync(draftConfig as Record<string, unknown>)
      queryClient.setQueryData(queryKeys.config.full, draftConfig)
      setSuccess('配置已保存并生效')
      setHasChanges(false)
      setTimeout(() => setSuccess(null), 3000)
    } catch (exc) {
      setLocalError(exc instanceof Error ? exc.message : '保存失败')
    }
  }

  function handleValueChange(path: string, input: string) {
    if (!draftConfig) return
    setLocalError(null)
    try {
      const previous = readByPath(draftConfig, path)
      const parsed = parseEditedValue(input, previous)
      const next = writeByPath(draftConfig, path, parsed) as ConfigObject
      setDraftConfig(next)
      setHasChanges(JSON.stringify(next) !== JSON.stringify(config))
    } catch (exc) {
      setLocalError(exc instanceof Error ? `${path}: ${exc.message}` : '配置值格式无效')
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
          <span style={{ color: comfyQuery.data?.available ? 'var(--color-success)' : 'var(--color-danger)' }}>
            {comfyQuery.isLoading ? '检测中' : comfyQuery.data?.available ? 'ComfyUI 可用' : 'ComfyUI 不可用'}
          </span>
        </div>
        <div className="flex flex-wrap gap-2 mb-4">
          <a className="btn btn-secondary" href={comfyQuery.data?.public_url} target="_blank" rel="noreferrer">
            <ExternalLink size={14} /> 打开 ComfyUI
          </a>
          <a className="btn btn-secondary" href={comfyQuery.data?.manager_url} target="_blank" rel="noreferrer">
            <ExternalLink size={14} /> 打开 Manager
          </a>
          {comfyQuery.data?.queue && (
            <span className="text-sm">队列：{comfyQuery.data.queue.running} 运行 / {comfyQuery.data.queue.pending} 等待</span>
          )}
        </div>
        <div className="space-y-2">
          {(Array.isArray(presetsQuery.data) ? presetsQuery.data : []).map(preset => {
            const missing = [...preset.validation.missing_models, ...preset.validation.missing_nodes]
            return (
              <div key={preset.id} className="flex items-start justify-between gap-3 text-sm">
                <span>{preset.name} <small>({preset.kind})</small></span>
                <span style={{ color: missing.length ? 'var(--color-warning)' : 'var(--color-success)' }}>
                  {missing.length ? `缺少：${missing.join('、')}` : '依赖完整'}
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
              按功能模块分组展示 config.yaml。第三列说明来自 config.yaml.template 行内注释；providers 中的 API Key 已脱敏显示。
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
