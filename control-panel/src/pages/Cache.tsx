import { useEffect, useState, useCallback } from 'react'
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend } from 'recharts'
import { Database, Trash2, RefreshCw, Settings } from 'lucide-react'
import Card from '@/components/Card'
import {
  getMetricsText, parseMetrics,
  getL3CacheConfig, updateL3CacheConfig, listL3Entries,
  updateL3EntryMode, deleteL3Entry, triggerL3Cleanup,
  type L3CacheConfig, type L3CacheEntry,
} from '@/api/client'

const initialCacheData = [
  { tier: 'L1', hits: 0 },
  { tier: 'L2', hits: 0 },
  { tier: 'L3', hits: 0 },
]

export default function Cache() {
  const [cacheData, setCacheData] = useState(initialCacheData)
  const [totalMisses, setTotalMisses] = useState(0)
  const [loading, setLoading] = useState(true)
  const [activeTab, setActiveTab] = useState<'overview' | 'l3-manage'>('overview')

  // L3 management state
  const [l3Config, setL3Config] = useState<L3CacheConfig | null>(null)
  const [l3Entries, setL3Entries] = useState<L3CacheEntry[]>([])
  const [l3Total, setL3Total] = useState(0)
  const [l3Page, setL3Page] = useState(1)
  const [l3Loading, setL3Loading] = useState(false)
  const [l3ModeFilter, setL3ModeFilter] = useState<string>('')
  const [cleanupRunning, setCleanupRunning] = useState(false)
  const [showConfigPanel, setShowConfigPanel] = useState(false)

  useEffect(() => {
    let cancelled = false

    async function load() {
      try {
        const text = await getMetricsText()
        const samples = parseMetrics(text)

        const hitSamples = samples.filter(s => s.name === 'gateway_cache_hits_total')
        const missValue = samples.find(s => s.name === 'gateway_cache_misses_total')?.value ?? 0

        const hitsByTier: Record<string, number> = {}
        hitSamples.forEach(s => {
          const tier = s.labels.tier || 'unknown'
          hitsByTier[tier] = (hitsByTier[tier] || 0) + s.value
        })

        // 语义修正(2026-07-06):
        // 后端只有一个 gateway_cache_misses_total(三级都没命中的次数),
        // 不存在"L1_misses / L2_misses / L3_misses"。旧代码用
        // (misses_total - l2_hits) 之类的减法算 tier-level misses,数字无
        // 物理意义(L2 hit 时 L1 也 miss,但后端不会计 misses_total)。
        // 现改为:每个 tier 只展示命中数,MISS 单独一个数字。
        if (!cancelled) {
          setCacheData([
            { tier: 'L1', hits: Math.round(hitsByTier['L1'] || 0) },
            { tier: 'L2', hits: Math.round(hitsByTier['L2'] || 0) },
            { tier: 'L3', hits: Math.round(hitsByTier['L3'] || 0) },
          ])
          setTotalMisses(Math.round(missValue))
          setLoading(false)
        }
      } catch {
        if (!cancelled) setLoading(false)
      }
    }

    load()
    const interval = setInterval(load, 10000)
    return () => { cancelled = true; clearInterval(interval) }
  }, [])

  // Load L3 config
  useEffect(() => {
    if (activeTab === 'l3-manage') {
      loadL3Config()
      loadL3Entries()
    }
  }, [activeTab])

  const loadL3Config = async () => {
    try {
      const resp = await getL3CacheConfig()
      setL3Config(resp.data)
    } catch (e) {
      console.error('Failed to load L3 config:', e)
    }
  }

  const loadL3Entries = useCallback(async () => {
    setL3Loading(true)
    try {
      const resp = await listL3Entries({
        page: l3Page,
        pageSize: 20,
        mode: l3ModeFilter || undefined,
      })
      setL3Entries(resp.data.items)
      setL3Total(resp.data.pagination.total)
    } catch (e) {
      console.error('Failed to load L3 entries:', e)
    } finally {
      setL3Loading(false)
    }
  }, [l3Page, l3ModeFilter])

  useEffect(() => {
    if (activeTab === 'l3-manage') {
      loadL3Entries()
    }
  }, [l3Page, l3ModeFilter, activeTab, loadL3Entries])

  const handleToggleMode = async (entry: L3CacheEntry) => {
    const newMode = entry.mode === 'auto' ? 'manual' : 'auto'
    try {
      await updateL3EntryMode(entry.id, newMode)
      loadL3Entries()
    } catch (e) {
      console.error('Failed to toggle mode:', e)
    }
  }

  const handleDeleteEntry = async (entry: L3CacheEntry) => {
    if (!confirm(`确定删除此缓存条目？\n${entry.promptPreview}`)) return
    try {
      await deleteL3Entry(entry.id)
      loadL3Entries()
    } catch (e) {
      console.error('Failed to delete entry:', e)
    }
  }

  const handleCleanup = async () => {
    setCleanupRunning(true)
    try {
      const resp = await triggerL3Cleanup()
      alert(`清理完成：删除 ${resp.data.deleted_count} 条过期条目`)
      loadL3Entries()
    } catch (e) {
      console.error('Failed to trigger cleanup:', e)
    } finally {
      setCleanupRunning(false)
    }
  }

  const handleSaveConfig = async (newConfig: Partial<L3CacheConfig>) => {
    try {
      const resp = await updateL3CacheConfig(newConfig)
      setL3Config(resp.data)
      setShowConfigPanel(false)
    } catch (e) {
      console.error('Failed to save config:', e)
    }
  }

  const totalHits = cacheData.reduce((s, d) => s + d.hits, 0)
  const total = totalHits + totalMisses
  const hitRate = total > 0 ? Math.round(totalHits / total * 100) : 0

  // 图表数据:每个 tier 一根 hits 柱 + 全局一根 MISS 柱
  const chartData = [
    ...cacheData,
    { tier: 'MISS', hits: totalMisses },
  ]

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-bold">缓存监控</h2>
        <div className="flex gap-2">
          <button
            onClick={() => setActiveTab('overview')}
            className="px-4 py-2 rounded-lg text-sm font-medium transition-colors"
            style={{
              backgroundColor: activeTab === 'overview' ? 'var(--color-primary)' : 'var(--color-bg-overlay)',
              color: activeTab === 'overview' ? 'white' : 'var(--color-text-secondary)',
              border: activeTab === 'overview' ? 'none' : '1px solid var(--color-border)',
            }}
          >
            概览
          </button>
          <button
            onClick={() => setActiveTab('l3-manage')}
            className="px-4 py-2 rounded-lg text-sm font-medium transition-colors"
            style={{
              backgroundColor: activeTab === 'l3-manage' ? 'var(--color-primary)' : 'var(--color-bg-overlay)',
              color: activeTab === 'l3-manage' ? 'white' : 'var(--color-text-secondary)',
              border: activeTab === 'l3-manage' ? 'none' : '1px solid var(--color-border)',
            }}
          >
            L3 缓存管理
          </button>
        </div>
      </div>

      {activeTab === 'overview' && (
        <>
          {/* 汇总 */}
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-5 gap-4">
            <Card>
              <div className="flex items-center gap-3 mb-2">
                <Database size={20} style={{ color: 'var(--color-primary)' }} />
                <span className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>总命中率</span>
              </div>
              <div className="text-3xl font-bold" style={{ color: 'var(--color-primary)' }}>
                {loading ? '...' : `${hitRate}%`}
              </div>
              <div className="text-sm mt-1" style={{ color: 'var(--color-text-tertiary)' }}>
                {total > 0 ? `${totalHits.toLocaleString()} / ${total.toLocaleString()}` : '等待请求数据...'}
              </div>
            </Card>
            <Card>
              <div className="flex items-center gap-3 mb-2">
                <span className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>L1 命中</span>
              </div>
              <div className="text-3xl font-bold" style={{ color: 'var(--color-success)' }}>
                {loading ? '...' : `${cacheData[0].hits}`}
              </div>
              <div className="text-sm mt-1" style={{ color: 'var(--color-text-tertiary)' }}>
                {total > 0 ? `${Math.round(cacheData[0].hits / total * 100)}% of ${total}` : '0%'}
              </div>
            </Card>
            <Card>
              <div className="flex items-center gap-3 mb-2">
                <span className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>L2 命中</span>
              </div>
              <div className="text-3xl font-bold" style={{ color: 'var(--color-warning)' }}>
                {loading ? '...' : `${cacheData[1].hits}`}
              </div>
              <div className="text-sm mt-1" style={{ color: 'var(--color-text-tertiary)' }}>
                {total > 0 ? `${Math.round(cacheData[1].hits / total * 100)}% of ${total}` : '0%'}
              </div>
            </Card>
            <Card>
              <div className="flex items-center gap-3 mb-2">
                <span className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>L3 语义命中</span>
              </div>
              <div className="text-3xl font-bold" style={{ color: 'var(--color-info)' }}>
                {loading ? '...' : `${cacheData[2].hits}`}
              </div>
              <div className="text-sm mt-1" style={{ color: 'var(--color-text-tertiary)' }}>
                {total > 0 ? `${Math.round(cacheData[2].hits / total * 100)}% of ${total}` : '0%'}
              </div>
            </Card>
            <Card>
              <div className="flex items-center gap-3 mb-2">
                <span className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>全部未命中</span>
              </div>
              <div className="text-3xl font-bold" style={{ color: 'var(--color-danger)' }}>
                {loading ? '...' : `${totalMisses}`}
              </div>
              <div className="text-sm mt-1" style={{ color: 'var(--color-text-tertiary)' }}>
                {total > 0 ? `${Math.round(totalMisses / total * 100)}% of ${total}` : '0%'}
              </div>
            </Card>
          </div>

          <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
            <Card title="各级缓存命中 vs 全局 MISS">
              <ResponsiveContainer width="100%" height={280}>
                <BarChart data={chartData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
                  <XAxis dataKey="tier" tick={{ fontSize: 12, fill: 'var(--color-text-secondary)' }} />
                  <YAxis tick={{ fontSize: 11, fill: 'var(--color-text-quaternary)' }} />
                  <Tooltip contentStyle={{ backgroundColor: 'var(--color-bg-elevated)', border: '1px solid var(--color-border)', borderRadius: 8 }} />
                  <Legend />
                  <Bar dataKey="hits" fill="var(--color-success)" name="Count" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </Card>

            <Card title="缓存架构说明">
              <div className="space-y-3 text-sm" style={{ color: 'var(--color-text-secondary)' }}>
                <div className="p-3 rounded-lg" style={{ backgroundColor: 'var(--color-bg-secondary)' }}>
                  <strong>L1 进程缓存</strong>: LRU, ≤1000 条, 单条 ≤100KB, 延迟 &lt;1ms
                </div>
                <div className="p-3 rounded-lg" style={{ backgroundColor: 'var(--color-bg-secondary)' }}>
                  <strong>L2 Redis 缓存</strong>: LZ4 压缩, 单条 ≤500KB, TTL 1h, 延迟 &lt;5ms
                </div>
                <div className="p-3 rounded-lg" style={{ backgroundColor: 'var(--color-bg-secondary)' }}>
                  <strong>L3 语义缓存</strong>: Qdrant 向量搜索, 近似匹配, TTL 24h
                </div>
                <div className="p-3 rounded-lg border" style={{ borderColor: 'var(--color-border)' }}>
                  <strong>回填策略</strong>: L2命中→回填L1 | L3命中→仅回填L1 | Miss→回填L1+L2+有条件L3
                </div>
              </div>
            </Card>
          </div>
        </>
      )}

      {activeTab === 'l3-manage' && (
        <div className="space-y-4">
          {/* 操作栏 */}
          <div className="flex items-center justify-between flex-wrap gap-3">
            <div className="flex items-center gap-3">
              <select
                value={l3ModeFilter}
                onChange={(e) => { setL3ModeFilter(e.target.value); setL3Page(1) }}
                className="px-3 py-2 rounded-lg border text-sm"
                style={{ borderColor: 'var(--color-border)', backgroundColor: 'var(--color-bg-elevated)', color: 'var(--color-text-primary)' }}
              >
                <option value="">全部模式</option>
                <option value="auto">自动过期</option>
                <option value="manual">手动管理</option>
              </select>
              <span className="text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
                共 {l3Total} 条
              </span>
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={() => setShowConfigPanel(!showConfigPanel)}
                className="flex items-center gap-1 px-3 py-2 rounded-lg text-sm border"
                style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-secondary)', backgroundColor: 'var(--color-bg-elevated)' }}
              >
                <Settings size={14} /> 配置
              </button>
              <button
                onClick={handleCleanup}
                disabled={cleanupRunning}
                className="flex items-center gap-1 px-3 py-2 rounded-lg text-sm disabled:opacity-50"
                style={{ backgroundColor: 'var(--color-warning)', color: 'white' }}
              >
                <RefreshCw size={14} className={cleanupRunning ? 'animate-spin' : ''} />
                {cleanupRunning ? '清理中...' : '立即清理过期'}
              </button>
            </div>
          </div>

          {/* 配置面板 */}
          {showConfigPanel && l3Config && (
            <Card title="L3 缓存配置">
              <L3ConfigForm config={l3Config} onSave={handleSaveConfig} onCancel={() => setShowConfigPanel(false)} />
            </Card>
          )}

          {/* 条目列表 */}
          <Card>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr style={{ borderBottom: '1px solid var(--color-border)' }}>
                    <th className="text-left py-3 px-2 font-medium" style={{ color: 'var(--color-text-secondary)' }}>Prompt 预览</th>
                    <th className="text-left py-3 px-2 font-medium" style={{ color: 'var(--color-text-secondary)' }}>模型</th>
                    <th className="text-left py-3 px-2 font-medium" style={{ color: 'var(--color-text-secondary)' }}>模式</th>
                    <th className="text-left py-3 px-2 font-medium" style={{ color: 'var(--color-text-secondary)' }}>命中次数</th>
                    <th className="text-left py-3 px-2 font-medium" style={{ color: 'var(--color-text-secondary)' }}>Token</th>
                    <th className="text-left py-3 px-2 font-medium" style={{ color: 'var(--color-text-secondary)' }}>过期时间</th>
                    <th className="text-left py-3 px-2 font-medium" style={{ color: 'var(--color-text-secondary)' }}>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {l3Loading ? (
                    <tr><td colSpan={7} className="py-8 text-center" style={{ color: 'var(--color-text-tertiary)' }}>加载中...</td></tr>
                  ) : l3Entries.length === 0 ? (
                    <tr><td colSpan={7} className="py-8 text-center" style={{ color: 'var(--color-text-tertiary)' }}>暂无缓存条目</td></tr>
                  ) : l3Entries.map((entry) => (
                    <tr key={entry.id} style={{ borderBottom: '1px solid var(--color-border)' }}>
                      <td className="py-2 px-2 max-w-[300px] truncate" title={entry.promptPreview}>
                        {entry.promptPreview || '(empty)'}
                      </td>
                      <td className="py-2 px-2">{entry.model}</td>
                      <td className="py-2 px-2">
                        <span
                          className="px-2 py-0.5 rounded text-xs font-medium"
                          style={{
                            backgroundColor: entry.mode === 'manual'
                              ? 'rgba(139, 92, 246, 0.15)'
                              : 'rgba(16, 185, 129, 0.15)',
                            color: entry.mode === 'manual'
                              ? 'var(--color-info, #8b5cf6)'
                              : 'var(--color-success)',
                          }}
                        >
                          {entry.mode === 'auto' ? '🔄 自动' : '📌 手动'}
                        </span>
                      </td>
                      <td className="py-2 px-2">{entry.hitCount}</td>
                      <td className="py-2 px-2">{entry.tokenCount}</td>
                      <td className="py-2 px-2 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                        {entry.expiresAt ? new Date(entry.expiresAt * 1000).toLocaleString() : '♾️ 永不过期'}
                      </td>
                      <td className="py-2 px-2">
                        <div className="flex items-center gap-1">
                          <button
                            onClick={() => handleToggleMode(entry)}
                            className="px-2 py-1 text-xs rounded border"
                            style={{
                              borderColor: 'var(--color-border)',
                              backgroundColor: 'var(--color-bg-overlay)',
                              color: 'var(--color-text-secondary)',
                            }}
                            title={entry.mode === 'auto' ? '切换为手动（永不过期）' : '切换为自动（按 TTL 过期）'}
                          >
                            {entry.mode === 'auto' ? '→ 手动' : '→ 自动'}
                          </button>
                          <button
                            onClick={() => handleDeleteEntry(entry)}
                            className="p-1 rounded"
                            style={{ color: 'var(--color-danger)' }}
                            title="删除"
                          >
                            <Trash2 size={14} />
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* 分页 */}
            {l3Total > 20 && (
              <div className="flex items-center justify-between mt-4 pt-3" style={{ borderTop: '1px solid var(--color-border)' }}>
                <span className="text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
                  第 {l3Page} 页 / 共 {Math.ceil(l3Total / 20)} 页
                </span>
                <div className="flex gap-2">
                  <button
                    onClick={() => setL3Page(p => Math.max(1, p - 1))}
                    disabled={l3Page === 1}
                    className="px-3 py-1 text-sm rounded border disabled:opacity-40"
                    style={{ borderColor: 'var(--color-border)', backgroundColor: 'var(--color-bg-overlay)', color: 'var(--color-text-secondary)' }}
                  >
                    上一页
                  </button>
                  <button
                    onClick={() => setL3Page(p => p + 1)}
                    disabled={l3Page >= Math.ceil(l3Total / 20)}
                    className="px-3 py-1 text-sm rounded border disabled:opacity-40"
                    style={{ borderColor: 'var(--color-border)', backgroundColor: 'var(--color-bg-overlay)', color: 'var(--color-text-secondary)' }}
                  >
                    下一页
                  </button>
                </div>
              </div>
            )}
          </Card>
        </div>
      )}
    </div>
  )
}

// L3 Config Form Component
function L3ConfigForm({ config, onSave, onCancel }: {
  config: L3CacheConfig
  onSave: (c: Partial<L3CacheConfig>) => void
  onCancel: () => void
}) {
  const [form, setForm] = useState(config)

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div>
          <label className="block text-sm font-medium mb-1" style={{ color: 'var(--color-text-secondary)' }}>默认模式</label>
          <select
            value={form.default_mode}
            onChange={(e) => setForm({ ...form, default_mode: e.target.value as 'auto' | 'manual' })}
            className="w-full px-3 py-2 rounded-lg border text-sm"
            style={{ borderColor: 'var(--color-border)', backgroundColor: 'var(--color-bg-elevated)', color: 'var(--color-text-primary)' }}
          >
            <option value="auto">自动过期 (auto)</option>
            <option value="manual">手动管理 (manual)</option>
          </select>
          <p className="text-xs mt-1" style={{ color: 'var(--color-text-quaternary)' }}>
            手动模式的条目不会被自动清理
          </p>
        </div>
        <div>
          <label className="block text-sm font-medium mb-1" style={{ color: 'var(--color-text-secondary)' }}>清理间隔 (分钟)</label>
          <input
            type="number"
            value={form.auto_cleanup_interval_minutes}
            onChange={(e) => setForm({ ...form, auto_cleanup_interval_minutes: Number(e.target.value) })}
            min={1}
            className="w-full px-3 py-2 rounded-lg border text-sm"
            style={{ borderColor: 'var(--color-border)', backgroundColor: 'var(--color-bg-elevated)', color: 'var(--color-text-primary)' }}
          />
        </div>
        <div>
          <label className="block text-sm font-medium mb-1" style={{ color: 'var(--color-text-secondary)' }}>默认 TTL (小时)</label>
          <input
            type="number"
            value={form.default_ttl_hours}
            onChange={(e) => setForm({ ...form, default_ttl_hours: Number(e.target.value) })}
            min={form.min_ttl_hours}
            max={form.max_ttl_hours}
            className="w-full px-3 py-2 rounded-lg border text-sm"
            style={{ borderColor: 'var(--color-border)', backgroundColor: 'var(--color-bg-elevated)', color: 'var(--color-text-primary)' }}
          />
        </div>
        <div>
          <label className="block text-sm font-medium mb-1" style={{ color: 'var(--color-text-secondary)' }}>最大 TTL (小时)</label>
          <input
            type="number"
            value={form.max_ttl_hours}
            onChange={(e) => setForm({ ...form, max_ttl_hours: Number(e.target.value) })}
            min={1}
            className="w-full px-3 py-2 rounded-lg border text-sm"
            style={{ borderColor: 'var(--color-border)', backgroundColor: 'var(--color-bg-elevated)', color: 'var(--color-text-primary)' }}
          />
        </div>
      </div>
      <div className="flex justify-end gap-2">
        <button
          onClick={onCancel}
          className="px-4 py-2 text-sm rounded-lg border"
          style={{ borderColor: 'var(--color-border)', backgroundColor: 'var(--color-bg-overlay)', color: 'var(--color-text-secondary)' }}
        >
          取消
        </button>
        <button
          onClick={() => onSave(form)}
          className="px-4 py-2 text-sm rounded-lg"
          style={{ backgroundColor: 'var(--color-primary)', color: 'white' }}
        >
          保存
        </button>
      </div>
    </div>
  )
}
