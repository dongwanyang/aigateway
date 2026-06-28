import { useEffect, useState } from 'react'
import { Puzzle } from 'lucide-react'
import Card from '@/components/Card'
import { getPluginsConfig, togglePlugin, getGlobalConfig, updateGlobalConfig } from '@/api/client'
import type { PluginConfigItem } from '@/api/client'

export default function Plugins() {
  const [plugins, setPlugins] = useState<PluginConfigItem[]>([])
  const [loading, setLoading] = useState(true)
  const [globalConfig, setGlobalConfig] = useState({ hot_reload: false, debug_mode: false })
  const [globalLoading, setGlobalLoading] = useState(true)

  useEffect(() => {
    Promise.all([
      getPluginsConfig().then(r => { setPlugins(r.data.plugins); setLoading(false) }),
      getGlobalConfig().then(r => { setGlobalConfig(r.data); setGlobalLoading(false) }),
    ]).catch(() => { setLoading(false); setGlobalLoading(false) })
  }, [])

  const toggle = async (name: string, currentEnabled: boolean) => {
    const newEnabled = !currentEnabled
    // Optimistic update
    setPlugins(prev => prev.map(p => p.name === name ? { ...p, enabled: newEnabled } : p))
    // Persist to backend
    try {
      await togglePlugin(name, newEnabled)
    } catch {
      // Rollback on failure
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

  const categories: Record<string, string> = {
    '安全': '安全',
    '性能': '性能',
    '路由': '路由',
    '优化': '优化',
  }

  const getCategory = (name: string): string => {
    if (name.includes('pii') || name.includes('detect')) return '安全'
    if (name.includes('cache') || name.includes('compress')) return '性能'
    if (name.includes('router')) return '路由'
    return '其他'
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-bold">插件管理</h2>
        <span className="text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
          {plugins.filter(p => p.enabled).length}/{plugins.length} 已启用
        </span>
      </div>

      {loading ? (
        <div className="space-y-3">
          {[1, 2, 3].map(i => <div key={i} className="h-16 skeleton rounded" />)}
        </div>
      ) : plugins.length === 0 ? (
        <Card>
          <div className="text-center py-8" style={{ color: 'var(--color-text-tertiary)' }}>
            未检测到插件配置
          </div>
        </Card>
      ) : (
        Object.entries(categories).map(([catKey, catLabel]) => {
          const catPlugins = plugins.filter(p => getCategory(p.name) === catKey)
          if (catPlugins.length === 0) return null
          return (
            <div key={catKey}>
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
