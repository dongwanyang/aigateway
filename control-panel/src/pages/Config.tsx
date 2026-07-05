import { useEffect, useState } from 'react'
import { Save, RefreshCw, AlertTriangle, Bug, Eye, Database, Globe, Network } from 'lucide-react'
import Card from '@/components/Card'
import { getFullConfig, updateFullConfig, getDebugConfig, updateDebugSection } from '@/api/client'
import type { DebugConfig } from '@/api/client'

export default function Config() {
  const [config, setConfig] = useState<Record<string, unknown> | null>(null)
  const [editText, setEditText] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [hasChanges, setHasChanges] = useState(false)

  // --- Debug 开关 ---
  const [debugCfg, setDebugCfg] = useState<DebugConfig | null>(null)
  const [debugLoading, setDebugLoading] = useState(true)

  useEffect(() => {
    loadConfig()
    loadDebug()
  }, [])

  async function loadDebug() {
    setDebugLoading(true)
    try {
      const cfg = await getDebugConfig()
      setDebugCfg(cfg)
    } catch {
      // non-fatal: debug config is optional
    } finally {
      setDebugLoading(false)
    }
  }

  async function toggleDimension(dim: keyof Pick<DebugConfig, 'frontend' | 'entry' | 'cache' | 'bridge' | 'plugins_enabled'>) {
    if (!debugCfg) return
    const newVal = !debugCfg[dim]
    // Optimistic update
    setDebugCfg(prev => prev ? { ...prev, [dim]: newVal } : prev)
    try {
      await updateDebugSection({ [dim]: newVal })
      await loadDebug() // reload to confirm
    } catch {
      await loadDebug() // rollback
    }
  }

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

      {/* 调试开关 */}
      <Card title="调试开关">
        {debugLoading ? (
          <div className="space-y-3">
            {[1, 2, 3, 4, 5].map(i => <div key={i} className="h-4 skeleton rounded" />)}
          </div>
        ) : debugCfg ? (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {([
              { key: 'frontend' as const, label: '前端', desc: 'ASGI 中间件层请求日志', icon: Globe },
              { key: 'entry' as const, label: '入口层', desc: '鉴权 + 分流 + 配额 + prompt_compress', icon: Eye },
              { key: 'cache' as const, label: '缓存', desc: 'L1/L2/L3 缓存读写', icon: Database },
              { key: 'bridge' as const, label: 'Bridge', desc: 'LiteLLM 模型调用出口', icon: Network },
              { key: 'plugins_enabled' as const, label: '插件总开关', desc: '所有插件 debug 日志', icon: Bug },
            ]).map(({ key, label, desc, icon: Icon }) => (
              <div
                key={key}
                className="flex items-center justify-between p-3 rounded-lg"
                style={{ backgroundColor: 'var(--color-bg-overlay)' }}
              >
                <div className="flex items-center gap-3">
                  <Icon size={18} style={{ color: debugCfg[key] ? 'var(--color-primary)' : 'var(--color-text-tertiary)' }} />
                  <div>
                    <div className="text-sm font-medium">{label}</div>
                    <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>{desc}</div>
                  </div>
                </div>
                <label className="toggle cursor-pointer">
                  <input
                    type="checkbox"
                    checked={!!debugCfg[key]}
                    onChange={() => toggleDimension(key)}
                  />
                  <span className="toggle-slider" />
                </label>
              </div>
            ))}
          </div>
        ) : (
          <div className="text-center py-4" style={{ color: 'var(--color-text-tertiary)' }}>
            无法加载调试配置
          </div>
        )}
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
