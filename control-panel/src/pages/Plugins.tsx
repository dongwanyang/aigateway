import { useEffect, useState } from 'react'
import { Puzzle, RefreshCw, Bug, Globe, Eye, Database, Network } from 'lucide-react'
import Card from '@/components/Card'
import {
  getPluginsConfig,
  togglePlugin,
  getGlobalConfig,
  updateGlobalConfig,
  setPluginDebug,
  getDebugConfig,
  updateDebugSection,
} from '@/api/client'
import type { PluginConfigItem, DebugConfig } from '@/api/client'
import { useAuth } from '@/contexts/AuthContext'

export default function Plugins() {
  const { isAuthenticated } = useAuth()
  const [plugins, setPlugins] = useState<PluginConfigItem[]>([])
  const [loading, setLoading] = useState(true)
  const [globalConfig, setGlobalConfig] = useState({ hot_reload: false })
  const [globalLoading, setGlobalLoading] = useState(true)
  const [debugCfg, setDebugCfg] = useState<DebugConfig | null>(null)
  const [debugLoading, setDebugLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

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
    } catch (err) {
      setError(err instanceof Error ? err.message : '插件配置加载失败')
    } finally {
      setLoading(false)
      setGlobalLoading(false)
    }
  }

  async function loadDebug() {
    setDebugLoading(true)
    try {
      setDebugCfg(await getDebugConfig())
    } catch {
      // non-fatal: debug config is optional
    } finally {
      setDebugLoading(false)
    }
  }

  useEffect(() => {
    if (!isAuthenticated) return
    void loadData()
    void loadDebug()
  }, [isAuthenticated])

  const toggle = async (name: string, currentEnabled: boolean) => {
    const next = !currentEnabled
    setPlugins(prev => prev.map(p => p.name === name ? { ...p, enabled: next } : p))
    try {
      await togglePlugin(name, next)
    } catch {
      setPlugins(prev => prev.map(p => p.name === name ? { ...p, enabled: currentEnabled } : p))
    }
  }

  const toggleDebug = async (name: string, currentDebug: boolean | null) => {
    if (currentDebug === null) return
    const next = !currentDebug
    setPlugins(prev => prev.map(p => p.name === name ? { ...p, debug: next } : p))
    try {
      await setPluginDebug(name, next)
    } catch {
      setPlugins(prev => prev.map(p => p.name === name ? { ...p, debug: currentDebug } : p))
    }
  }

  const toggleGlobal = async () => {
    const next = !globalConfig.hot_reload
    setGlobalConfig({ hot_reload: next })
    try {
      await updateGlobalConfig({ hot_reload: next })
    } catch {
      setGlobalConfig({ hot_reload: !next })
    }
  }

  async function toggleDebugDimension(
    dim: keyof Pick<DebugConfig, 'frontend' | 'entry' | 'cache' | 'bridge' | 'plugins_enabled'>,
  ) {
    if (!debugCfg) return
    const next = !debugCfg[dim]
    setDebugCfg(prev => prev ? { ...prev, [dim]: next } : prev)
    try {
      await updateDebugSection({ [dim]: next })
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

  if (!isAuthenticated) {
    return <div style={{ color: 'var(--color-text-tertiary)' }}>请先登录控制台。</div>
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
            onClick={() => void loadData()}
            style={{
              display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
              padding: '6px 10px', borderRadius: '6px', border: '1px solid var(--color-border)',
              cursor: 'pointer', fontSize: '12px', backgroundColor: 'var(--color-bg-overlay)',
              color: 'var(--color-text-secondary)',
            }}
            title="刷新数据"
          >
            <RefreshCw size={14} />
          </button>
        </div>
      </div>

      {error && (
        <Card style={{ borderLeft: '4px solid var(--color-danger)', backgroundColor: 'var(--color-error-bg)' }}>
          <div className="flex items-center justify-between">
            <span className="text-sm" style={{ color: 'var(--color-danger)' }}>{error}</span>
            <button
              onClick={() => { setError(null); void loadData() }}
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
        <Card><div className="text-center py-8" style={{ color: 'var(--color-text-tertiary)' }}>未检测到插件配置</div></Card>
      ) : (
        (['understanding', 'generation'] as const).map(kind => {
          const kindPlugins = plugins.filter(p => (p.pipeline_kind || 'understanding') === kind)
          if (kindPlugins.length === 0) return null
          return (
            <div key={kind} className="mb-8">
              <h3 className="text-lg font-semibold mb-4" style={{ color: 'var(--color-text-primary)' }}>
                {kind === 'understanding' ? '理解管道' : '生成管道'}
                <span className="ml-2 text-sm font-normal" style={{ color: 'var(--color-text-tertiary)' }}>({kindPlugins.length} 插件)</span>
              </h3>
              {['缓存', '安全', '性能', '路由', '其他'].map(catLabel => {
                const catPlugins = kindPlugins.filter(p => getCategory(p.name) === catLabel)
                if (catPlugins.length === 0) return null
                return (
                  <div key={catLabel} className="mb-4">
                    <div className="text-sm font-medium mb-2" style={{ color: 'var(--color-text-secondary)' }}>{catLabel}</div>
                    <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
                      {catPlugins.map(plugin => (
                        <Card key={plugin.name} className="flex items-center justify-between">
                          <div className="flex items-center gap-3">
                            <div className="p-2 rounded-lg" style={{ backgroundColor: plugin.enabled ? 'var(--color-primary)' : 'var(--color-bg-overlay)' }}>
                              <Puzzle size={20} style={{ color: plugin.enabled ? 'white' : 'var(--color-text-tertiary)' }} />
                            </div>
                            <div>
                              <div className="font-medium">{plugin.name}</div>
                              <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>{getPluginDescription(plugin.name)}</div>
                            </div>
                          </div>
                          <div className="flex items-center gap-2">
                            {plugin.debug !== null && plugin.debug !== undefined && (
                              <button
                                onClick={() => void toggleDebug(plugin.name, plugin.debug ?? false)}
                                title="Debug 日志"
                                className="p-2 rounded-lg cursor-pointer"
                                style={{ backgroundColor: plugin.debug ? 'var(--color-warning, #f59e0b)' : 'var(--color-bg-overlay)' }}
                              >
                                <Bug size={16} style={{ color: plugin.debug ? 'white' : 'var(--color-text-tertiary)' }} />
                              </button>
                            )}
                            <label className="toggle cursor-pointer">
                              <input type="checkbox" checked={plugin.enabled} onChange={() => void toggle(plugin.name, plugin.enabled)} />
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
          <div className="space-y-3">{[1, 2, 3, 4, 5].map(i => <div key={i} className="h-4 skeleton rounded" />)}</div>
        ) : (
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <div>
                <div className="text-sm font-medium">热重载</div>
                <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>修改 config.yaml 后自动生效</div>
              </div>
              <label className="toggle cursor-pointer">
                <input type="checkbox" checked={globalConfig.hot_reload} onChange={() => void toggleGlobal()} disabled={globalLoading} />
                <span className="toggle-slider" />
              </label>
            </div>
            <hr style={{ borderColor: 'var(--color-border)' }} />
            <div className="text-sm font-medium mb-1" style={{ color: 'var(--color-text-secondary)' }}>分维度调试开关</div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              {([
                { key: 'frontend' as const, label: '前端', desc: 'ASGI 中间件层请求日志', icon: Globe },
                { key: 'entry' as const, label: '入口层', desc: '鉴权 + 分流 + 配额 + prompt_compress', icon: Eye },
                { key: 'cache' as const, label: '缓存', desc: 'L1/L2/L3 缓存读写', icon: Database },
                { key: 'bridge' as const, label: 'Bridge', desc: 'LiteLLM 模型调用出口', icon: Network },
                { key: 'plugins_enabled' as const, label: '插件总开关', desc: '所有插件 debug 日志', icon: Bug },
              ]).map(({ key, label, desc, icon: Icon }) => (
                <div key={key} className="flex items-center justify-between p-3 rounded-lg" style={{ backgroundColor: 'var(--color-bg-overlay)' }}>
                  <div className="flex items-center gap-3">
                    <Icon size={18} style={{ color: debugCfg?.[key] ? 'var(--color-primary)' : 'var(--color-text-tertiary)' }} />
                    <div><div className="text-sm font-medium">{label}</div><div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>{desc}</div></div>
                  </div>
                  <label className="toggle cursor-pointer">
                    <input type="checkbox" checked={!!debugCfg?.[key]} onChange={() => void toggleDebugDimension(key)} />
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
