import { useEffect, useState } from 'react'
import { Puzzle, Key, Save, X, RefreshCw, Bug, Globe, Eye, Database, Network } from 'lucide-react'
import Card from '@/components/Card'
import {
  getPluginsConfig,
  togglePlugin,
  getGlobalConfig,
  updateGlobalConfig,
  setPluginDebug,
  saveApiKey,
  getSavedApiKey,
  getDebugConfig,
  updateDebugSection,
} from '@/api/client'
import type { PluginConfigItem, DebugConfig } from '@/api/client'

export default function Plugins() {
  const [plugins, setPlugins] = useState<PluginConfigItem[]>([])
  const [loading, setLoading] = useState(true)
  const [globalConfig, setGlobalConfig] = useState({ hot_reload: false })
  const [globalLoading, setGlobalLoading] = useState(true)
  const [debugCfg, setDebugCfg] = useState<DebugConfig | null>(null)
  const [debugLoading, setDebugLoading] = useState(true)
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
      setGlobalConfig({ hot_reload: globalRes.data.hot_reload })
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

  useEffect(() => {
    loadData()
    loadDebug()
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

  const toggleDebug = async (name: string, currentDebug: boolean | null) => {
    if (currentDebug === null) return  // prompt_compress 等不支持单独 debug
    const newDebug = !currentDebug
    setPlugins(prev => prev.map(p => p.name === name ? { ...p, debug: newDebug } : p))
    try {
      await setPluginDebug(name, newDebug)
    } catch {
      setPlugins(prev => prev.map(p => p.name === name ? { ...p, debug: currentDebug } : p))
    }
  }

  const toggleGlobal = async (key: 'hot_reload', currentValue: boolean) => {
    const newValue = !currentValue
    setGlobalConfig(prev => ({ ...prev, [key]: newValue }))
    try {
      // Only send the toggled field — backend preserves debug_mode when omitted
      // (update_global_config falls back to the current value). Sending
      // debug_mode: false here would silently disable debug mode on every toggle.
      await updateGlobalConfig({
        hot_reload: newValue,
      })
    } catch {
      setGlobalConfig(prev => ({ ...prev, [key]: currentValue }))
    }
  }

  async function toggleDebugDimension(dim: keyof Pick<DebugConfig, 'frontend' | 'entry' | 'cache' | 'bridge' | 'plugins_enabled'>) {
    if (!debugCfg) return
    const newVal = !debugCfg[dim]
    setDebugCfg(prev => prev ? { ...prev, [dim]: newVal } : prev)
    try {
      await updateDebugSection({ [dim]: newVal })
      await loadDebug()
    } catch {
      await loadDebug()
    }
  }

  const getCategory = (name: string): string => {
    if (name.includes('pii') || name.includes('detect')) return '安全'
    if (name.includes('cache')) return '缓存'
    if (name.includes('compress')) return '性能'
    if (name.includes('router')) return '路由'
    return '其他'
  }

  const getPluginDescription = (name: string): string => {
    const descriptions: Record<string, string> = {
      pii_detector: 'PII 敏感信息检测与脱敏',
      prompt_cache: 'Prompt 精确匹配缓存 (L1 进程 + L2 Redis)',
      semantic_cache: '语义相似度向量缓存 (L3 Qdrant)',
      model_router: '多模型智能路由分发',
      prompt_compress: 'Prompt 压缩以降低 Token 消耗',
    }
    return descriptions[name] ?? '默认配置'
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
        (['understanding', 'generation'] as const).map(kind => {
          const kindPlugins = plugins.filter(p => (p.pipeline_kind || 'understanding') === kind)
          if (kindPlugins.length === 0) return null
          return (
            <div key={kind} className="mb-8">
              <h3 className="text-lg font-semibold mb-4" style={{ color: 'var(--color-text-primary)' }}>
                {kind === 'understanding' ? '理解管道' : '生成管道'}
                <span className="ml-2 text-sm font-normal" style={{ color: 'var(--color-text-tertiary)' }}>
                  ({kindPlugins.length} 插件)
                </span>
              </h3>
              {['缓存', '安全', '性能', '路由', '其他'].map(catLabel => {
                const catPlugins = kindPlugins.filter(p => getCategory(p.name) === catLabel)
                if (catPlugins.length === 0) return null
                return (
                  <div key={catLabel} className="mb-4">
                    <div className="text-sm font-medium mb-2" style={{ color: 'var(--color-text-secondary)' }}>
                      {catLabel}
                    </div>
                    <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
                      {catPlugins.map(plugin => (
                        <Card key={plugin.name} className="flex items-center justify-between">
                          <div className="flex items-center gap-3">
                            <div className="p-2 rounded-lg" style={{ backgroundColor: plugin.enabled ? 'var(--color-primary)' : 'var(--color-bg-overlay)' }}>
                              <Puzzle size={20} style={{ color: plugin.enabled ? 'white' : 'var(--color-text-tertiary)' }} />
                            </div>
                            <div>
                              <div className="font-medium flex items-center gap-2">
                                {plugin.name}
                                {plugin.pipeline_kind && (
                                  <span
                                    className="text-xs px-2 py-0.5 rounded"
                                    style={{
                                      backgroundColor: plugin.pipeline_kind === 'generation'
                                        ? 'var(--color-warning, #f59e0b)'
                                        : 'var(--color-bg-overlay)',
                                      color: plugin.pipeline_kind === 'generation' ? 'white' : 'var(--color-text-tertiary)',
                                    }}
                                    title={plugin.pipeline_kind === 'generation' ? '生成管道' : '理解管道'}
                                  >
                                    {plugin.pipeline_kind === 'generation' ? '生成' : '理解'}
                                  </span>
                                )}
                              </div>
                              <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                                {getPluginDescription(plugin.name)}
                              </div>
                            </div>
                          </div>
                          <div className="flex items-center gap-2">
                            {plugin.debug !== null && plugin.debug !== undefined && (
                              <button
                                onClick={() => toggleDebug(plugin.name, plugin.debug ?? false)}
                                title="Debug 日志"
                                className="p-2 rounded-lg cursor-pointer"
                                style={{
                                  backgroundColor: plugin.debug ? 'var(--color-warning, #f59e0b)' : 'var(--color-bg-overlay)',
                                }}
                              >
                                <Bug size={16} style={{ color: plugin.debug ? 'white' : 'var(--color-text-tertiary)' }} />
                              </button>
                            )}
                            <label className="toggle cursor-pointer">
                              <input
                                type="checkbox"
                                checked={plugin.enabled}
                                onChange={() => toggle(plugin.name, plugin.enabled)}
                              />
                              <span className="toggle-slider" />
                            </label>
                          </div>
                        </Card>
                      ))}
                    </div>
                  </div>
                )
              })}
            </div>
          )
        })
      )}

      <Card title="全局配置">
        {debugLoading ? (
          <div className="space-y-3">
            {[1, 2, 3, 4, 5].map(i => <div key={i} className="h-4 skeleton rounded" />)}
          </div>
        ) : (
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
            <hr style={{ borderColor: 'var(--color-border)' }} />
            <div className="text-sm font-medium mb-1" style={{ color: 'var(--color-text-secondary)' }}>
              分维度调试开关
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
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
                    <Icon size={18} style={{ color: debugCfg?.[key] ? 'var(--color-primary)' : 'var(--color-text-tertiary)' }} />
                    <div>
                      <div className="text-sm font-medium">{label}</div>
                      <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>{desc}</div>
                    </div>
                  </div>
                  <label className="toggle cursor-pointer">
                    <input
                      type="checkbox"
                      checked={!!debugCfg?.[key]}
                      onChange={() => toggleDebugDimension(key)}
                    />
                    <span className="toggle-slider" />
                  </label>
                </div>
              ))}
            </div>
          </div>
        )}
      </Card>
    </div>
  )
}
