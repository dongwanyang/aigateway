import { useEffect, useState } from 'react'
import { Puzzle, Key, Save, X, RefreshCw } from 'lucide-react'
import Card from '@/components/Card'
import {
  getPluginsConfig,
  togglePlugin,
  getGlobalConfig,
  updateGlobalConfig,
  saveApiKey,
  getSavedApiKey,
} from '@/api/client'
import type { PluginConfigItem } from '@/api/client'

export default function Plugins() {
  const [plugins, setPlugins] = useState<PluginConfigItem[]>([])
  const [loading, setLoading] = useState(true)
  const [globalConfig, setGlobalConfig] = useState({ hot_reload: false, debug_mode: false })
  const [globalLoading, setGlobalLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // --- API Key 管理 ---
  const [apiKeyInput, setApiKeyInput] = useState('')
  const [showKeyInput, setShowKeyInput] = useState(false)
  const [savedKey, setSavedKey] = useState(getSavedApiKey())

  useEffect(() => {
    setApiKeyInput(savedKey ?? '')
  }, [savedKey])

  const handleSaveKey = () => {
    if (apiKeyInput.trim()) {
      saveApiKey(apiKeyInput.trim())
      setSavedKey(apiKeyInput.trim())
      setShowKeyInput(false)
      setError(null)
      loadData()
    }
  }

  // --- 数据加载（带鉴权重试） ---
  async function loadData() {
    setLoading(true)
    setGlobalLoading(true)
    setError(null)

    try {
      const [pluginsRes, globalRes] = await Promise.all([
        getPluginsConfig(),
        getGlobalConfig(),
      ])
      setPlugins(pluginsRes.data.plugins)
      setGlobalConfig(globalRes.data)
    } catch {
      if (savedKey) {
        setError('API Key 无效或服务不可用，请重新输入')
      } else {
        setError('未配置 API Key，请先输入管理员密钥')
      }
    } finally {
      setLoading(false)
      setGlobalLoading(false)
    }
  }

  useEffect(() => {
    loadData()
  }, [])

  const toggle = async (name: string, currentEnabled: boolean) => {
    const newEnabled = !currentEnabled
    setPlugins(prev => prev.map(p => p.name === name ? { ...p, enabled: newEnabled } : p))
    try {
      await togglePlugin(name, newEnabled)
    } catch {
      setPlugins(prev => prev.map(p => p.name === name ? { ...p, enabled: currentEnabled } : p))
    }
  }

  const toggleGlobal = async (key: 'hot_reload' | 'debug_mode', currentValue: boolean) => {
    const newValue = !currentValue
    setGlobalConfig(prev => ({ ...prev, [key]: newValue }))
    try {
      await updateGlobalConfig({
        hot_reload: key === 'hot_reload' ? newValue : globalConfig.hot_reload,
        debug_mode: key === 'debug_mode' ? newValue : globalConfig.debug_mode,
      })
    } catch {
      setGlobalConfig(prev => ({ ...prev, [key]: currentValue }))
    }
  }

  const getCategory = (name: string): string => {
    if (name.includes('pii') || name.includes('detect')) return '安全'
    if (name.includes('cache') || name.includes('compress')) return '性能'
    if (name.includes('router')) return '路由'
    return '其他'
  }

  // 如果还没有保存的 API Key，显示输入界面
  if (!savedKey && !showKeyInput) {
    return (
      <div className="space-y-6">
        <div className="flex items-center justify-between">
          <h2 className="text-2xl font-bold">插件管理</h2>
        </div>

        <Card>
          <div className="max-w-md mx-auto text-center py-8">
            <Key size={48} className="mx-auto mb-4 opacity-40" style={{ color: 'var(--color-primary)' }} />
            <h3 className="text-lg font-semibold mb-2">需要 API Key</h3>
            <p className="text-sm mb-6" style={{ color: 'var(--color-text-tertiary)' }}>
              请输入管理员 API Key 以查看和管理插件配置。
              <br />
              密钥将保存在浏览器本地存储中。
            </p>
            <button
              onClick={() => setShowKeyInput(true)}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: '8px',
                padding: '10px 24px',
                borderRadius: '8px',
                border: 'none',
                cursor: 'pointer',
                fontWeight: 600,
                fontSize: '14px',
                backgroundColor: 'var(--color-primary)',
                color: 'white',
              }}
            >
              <Key size={16} />
              输入 API Key
            </button>
          </div>
        </Card>
      </div>
    )
  }

  // 正在输入 API Key
  if (showKeyInput) {
    return (
      <div className="space-y-6">
        <div className="flex items-center justify-between">
          <h2 className="text-2xl font-bold">插件管理</h2>
        </div>

        <Card>
          <div className="max-w-md mx-auto py-6">
            <div className="flex items-center gap-3 mb-4">
              <Key size={24} style={{ color: 'var(--color-primary)' }} />
              <h3 className="text-lg font-semibold">配置 API Key</h3>
            </div>
            <div className="flex gap-2">
              <input
                type="password"
                value={apiKeyInput}
                onChange={e => setApiKeyInput(e.target.value)}
                placeholder="sk-xxxxxxxx..."
                onKeyDown={e => { if (e.key === 'Enter') handleSaveKey() }}
                style={{
                  flex: 1,
                  padding: '10px 14px',
                  borderRadius: '8px',
                  border: '1px solid var(--color-border)',
                  backgroundColor: 'var(--color-bg-base)',
                  color: 'var(--color-text-primary)',
                  fontSize: '14px',
                  outline: 'none',
                }}
                autoFocus
              />
              <button
                onClick={handleSaveKey}
                disabled={!apiKeyInput.trim()}
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: '6px',
                  padding: '10px 18px',
                  borderRadius: '8px',
                  border: 'none',
                  cursor: apiKeyInput.trim() ? 'pointer' : 'not-allowed',
                  fontWeight: 600,
                  fontSize: '14px',
                  backgroundColor: apiKeyInput.trim() ? 'var(--color-primary)' : 'var(--color-bg-overlay)',
                  color: apiKeyInput.trim() ? 'white' : 'var(--color-text-tertiary)',
                }}
              >
                <Save size={16} />
                保存
              </button>
              <button
                onClick={() => { setShowKeyInput(false); setApiKeyInput(savedKey ?? '') }}
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  padding: '10px 14px',
                  borderRadius: '8px',
                  border: '1px solid var(--color-border)',
                  cursor: 'pointer',
                  backgroundColor: 'var(--color-bg-overlay)',
                  color: 'var(--color-text-secondary)',
                }}
              >
                <X size={16} />
              </button>
            </div>
            <p className="text-xs mt-3" style={{ color: 'var(--color-text-tertiary)' }}>
              默认管理员 Key: sk-a1b2c3d4e5f6XDFDDSF12nco
            </p>
          </div>
        </Card>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-bold">插件管理</h2>
        <div className="flex items-center gap-3">
          <span className="text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
            {plugins.filter(p => p.enabled).length}/{plugins.length} 已启用
          </span>
          <button
            onClick={() => { setShowKeyInput(true); setApiKeyInput(savedKey ?? '') }}
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: '6px',
              padding: '6px 12px',
              borderRadius: '6px',
              border: '1px solid var(--color-border)',
              cursor: 'pointer',
              fontSize: '12px',
              backgroundColor: 'var(--color-bg-overlay)',
              color: 'var(--color-text-secondary)',
            }}
            title="更换 API Key"
          >
            <Key size={14} />
            更换 Key
          </button>
          <button
            onClick={loadData}
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
              padding: '6px 10px',
              borderRadius: '6px',
              border: '1px solid var(--color-border)',
              cursor: 'pointer',
              fontSize: '12px',
              backgroundColor: 'var(--color-bg-overlay)',
              color: 'var(--color-text-secondary)',
            }}
            title="刷新数据"
          >
            <RefreshCw size={14} />
          </button>
        </div>
      </div>

      {/* 错误提示 */}
      {error && (
        <Card style={{ borderLeft: '4px solid var(--color-danger)', backgroundColor: 'var(--color-error-bg)' }}>
          <div className="flex items-center justify-between">
            <span className="text-sm" style={{ color: 'var(--color-danger)' }}>{error}</span>
            <button
              onClick={() => { setError(null); loadData() }}
              style={{ color: 'var(--color-danger)', background: 'none', border: 'none', cursor: 'pointer', fontSize: '12px' }}
            >
              重试
            </button>
          </div>
        </Card>
      )}

      {loading ? (
        <div className="space-y-3">
          {[1, 2, 3].map(i => <div key={i} className="h-16 skeleton rounded" />)}
        </div>
      ) : plugins.length === 0 ? (
        <Card>
          <div className="text-center py-8" style={{ color: 'var(--color-text-tertiary)' }}>
            {error ? '点击重试加载插件配置' : '未检测到插件配置'}
          </div>
        </Card>
      ) : (
        ['安全', '性能', '路由', '其他'].map(catLabel => {
          const catPlugins = plugins.filter(p => getCategory(p.name) === catLabel)
          if (catPlugins.length === 0) return null
          return (
            <div key={catLabel}>
              <h3 className="text-sm font-semibold mb-3 uppercase tracking-wide" style={{ color: 'var(--color-text-tertiary)' }}>
                {catLabel}
              </h3>
              <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
                {catPlugins.map(plugin => (
                  <Card key={plugin.name} className="flex items-center justify-between">
                    <div className="flex items-center gap-3">
                      <div className="p-2 rounded-lg" style={{ backgroundColor: plugin.enabled ? 'var(--color-primary)' : 'var(--color-bg-overlay)' }}>
                        <Puzzle size={20} style={{ color: plugin.enabled ? 'white' : 'var(--color-text-tertiary)' }} />
                      </div>
                      <div>
                        <div className="font-medium">{plugin.name}</div>
                        <div className="text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
                          {Object.keys(plugin.config).length > 0 ? JSON.stringify(plugin.config) : '默认配置'}
                        </div>
                      </div>
                    </div>
                    <label className="toggle cursor-pointer">
                      <input
                        type="checkbox"
                        checked={plugin.enabled}
                        onChange={() => toggle(plugin.name, plugin.enabled)}
                      />
                      <span className="toggle-slider" />
                    </label>
                  </Card>
                ))}
              </div>
            </div>
          )
        })
      )}

      <Card title="全局配置">
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-sm font-medium">热重载</div>
              <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>修改 config.yaml 后自动生效</div>
            </div>
            <label className="toggle cursor-pointer">
              <input
                type="checkbox"
                checked={globalConfig.hot_reload}
                onChange={() => toggleGlobal('hot_reload', globalConfig.hot_reload)}
                disabled={globalLoading}
              />
              <span className="toggle-slider" />
            </label>
          </div>
          <div className="flex items-center justify-between">
            <div>
              <div className="text-sm font-medium">调试模式</div>
              <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>记录详细插件执行日志</div>
            </div>
            <label className="toggle cursor-pointer">
              <input
                type="checkbox"
                checked={globalConfig.debug_mode}
                onChange={() => toggleGlobal('debug_mode', globalConfig.debug_mode)}
                disabled={globalLoading}
              />
              <span className="toggle-slider" />
            </label>
          </div>
        </div>
      </Card>
    </div>
  )
}
