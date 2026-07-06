import { useEffect, useState } from 'react'
import { Save, RefreshCw, AlertTriangle } from 'lucide-react'
import Card from '@/components/Card'
import { getFullConfig, updateFullConfig } from '@/api/client'

export default function Config() {
  const [config, setConfig] = useState<Record<string, unknown> | null>(null)
  const [editText, setEditText] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [hasChanges, setHasChanges] = useState(false)

  useEffect(() => {
    loadConfig()
  }, [])

  async function loadConfig() {
    setLoading(true)
    setError(null)
    try {
      const r = await getFullConfig()
      setConfig(r.data as Record<string, unknown>)
      const formatted = JSON.stringify(r.data, null, 2)
      setEditText(formatted)
      setHasChanges(false)
    } catch (e: any) {
      setError(e.message || '加载配置失败')
    } finally {
      setLoading(false)
    }
  }

  async function handleSave() {
    setError(null)
    setSuccess(null)

    // 验证 JSON 格式
    let parsed: Record<string, unknown>
    try {
      parsed = JSON.parse(editText)
    } catch {
      setError('JSON 格式无效，请检查语法')
      return
    }

    setSaving(true)
    try {
      await updateFullConfig(parsed)
      setSuccess('配置已保存并生效')
      setConfig(parsed)
      setHasChanges(false)
      setTimeout(() => setSuccess(null), 3000)
    } catch (e: any) {
      setError(e.message || '保存失败')
    } finally {
      setSaving(false)
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
