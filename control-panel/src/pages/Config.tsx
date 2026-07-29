import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Save, RefreshCw, AlertTriangle, ExternalLink } from 'lucide-react'
import Card from '@/components/Card'
import { getComfyUIStatus, getFullConfig, getGenerationPresets, updateFullConfig } from '@/api/client'
import { queryKeys } from '@/query/keys'

export default function Config() {
  const queryClient = useQueryClient()
  const [editText, setEditText] = useState('')
  const [localError, setLocalError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [hasChanges, setHasChanges] = useState(false)
  const configQuery = useQuery({
    queryKey: queryKeys.config.full,
    queryFn: async () => (await getFullConfig()).data as Record<string, unknown>,
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
  const saveMutation = useMutation({
    mutationFn: updateFullConfig,
  })
  const config = configQuery.data ?? null
  const loading = configQuery.isLoading
  const saving = saveMutation.isPending
  const remoteError = configQuery.error ?? saveMutation.error
  const error = localError ?? (remoteError instanceof Error ? remoteError.message : null)

  useEffect(() => {
    if (config && !hasChanges) {
      setEditText(JSON.stringify(config, null, 2))
    }
  }, [config, hasChanges])

  async function loadConfig() {
    setLocalError(null)
    setHasChanges(false)
    await configQuery.refetch()
  }

  async function handleSave() {
    setLocalError(null)
    setSuccess(null)

    // 验证 JSON 格式
    let parsed: Record<string, unknown>
    try {
      parsed = JSON.parse(editText)
    } catch {
      setLocalError('JSON 格式无效，请检查语法')
      return
    }

    try {
      await saveMutation.mutateAsync(parsed)
      queryClient.setQueryData(queryKeys.config.full, parsed)
      setSuccess('配置已保存并生效')
      setHasChanges(false)
      setTimeout(() => setSuccess(null), 3000)
    } catch (exc) {
      setLocalError(exc instanceof Error ? exc.message : '保存失败')
    }
  }

  function handleTextChange(value: string) {
    setEditText(value)
    setHasChanges(value !== JSON.stringify(config, null, 2))
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-bold">系统配置</h2>
        <div className="flex items-center gap-2">
          <button
            className="btn btn-secondary"
            style={{ padding: '8px 14px', fontSize: '12px' }}
            onClick={loadConfig}
            disabled={loading}
          >
            <RefreshCw size={14} /> 重新加载
          </button>
          <button
            className="btn btn-primary"
            style={{ padding: '8px 14px', fontSize: '12px' }}
            onClick={handleSave}
            disabled={saving || !hasChanges}
          >
            <Save size={14} /> {saving ? '保存中...' : '保存配置'}
          </button>
        </div>
      </div>

      {/* 提示信息 */}
      {hasChanges && (
        <div style={{
          padding: '10px 16px',
          borderRadius: '8px',
          backgroundColor: 'rgba(245, 158, 11, 0.1)',
          border: '1px solid var(--color-warning)',
          fontSize: '13px',
          color: 'var(--color-warning)',
          display: 'flex',
          alignItems: 'center',
          gap: '8px',
        }}>
          <AlertTriangle size={14} />
          配置已修改但未保存。点击"保存配置"使变更生效。
        </div>
      )}

      {error && (
        <div style={{
          padding: '10px 16px',
          borderRadius: '8px',
          backgroundColor: 'rgba(239, 68, 68, 0.1)',
          border: '1px solid var(--color-danger)',
          fontSize: '13px',
          color: 'var(--color-danger)',
        }}>
          ❌ {error}
        </div>
      )}

      {success && (
        <div style={{
          padding: '10px 16px',
          borderRadius: '8px',
          backgroundColor: 'rgba(16, 185, 129, 0.1)',
          border: '1px solid var(--color-success)',
          fontSize: '13px',
          color: 'var(--color-success)',
        }}>
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

      {/* 配置编辑器 */}
      <Card>
        {loading ? (
          <div className="space-y-3">
            {[1, 2, 3, 4, 5].map(i => <div key={i} className="h-4 skeleton rounded" />)}
          </div>
        ) : (
          <div>
            <div className="flex items-center justify-between mb-2">
              <span className="text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
                config.yaml (JSON 格式编辑)
              </span>
              <span className="text-xs" style={{ color: 'var(--color-text-quaternary)' }}>
                注: providers 中的 API Key 已脱敏显示
              </span>
            </div>
            <textarea
              value={editText}
              onChange={e => handleTextChange(e.target.value)}
              style={{
                width: '100%',
                minHeight: '500px',
                padding: '16px',
                fontFamily: 'var(--font-mono)',
                fontSize: '13px',
                lineHeight: '1.6',
                borderRadius: '8px',
                border: '1px solid var(--color-border)',
                backgroundColor: 'var(--color-bg-input)',
                color: 'var(--color-text-primary)',
                resize: 'vertical',
                outline: 'none',
                tabSize: 2,
              }}
              spellCheck={false}
            />
          </div>
        )}
      </Card>
    </div>
  )
}
