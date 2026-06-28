import { useEffect, useState } from 'react'
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend } from 'recharts'
import { Database } from 'lucide-react'
import Card from '@/components/Card'
import { getMetricsText, parseMetrics } from '@/api/client'

const initialCacheData = [
  { tier: 'L1', hits: 0, misses: 0 },
  { tier: 'L2', hits: 0, misses: 0 },
  { tier: 'L3', hits: 0, misses: 0 },
]

export default function Cache() {
  const [cacheData, setCacheData] = useState(initialCacheData)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false

    async function load() {
      try {
        const text = await getMetricsText()
        const samples = parseMetrics(text)

        const hitSamples = samples.filter(s => s.name === 'gateway_cache_hits_total')
        const missValue = samples.find(s => s.name === 'gateway_cache_misses_total')?.value ?? 0

        // 按 tier 分类 hits
        const hitsByTier: Record<string, number> = {}
        hitSamples.forEach(s => {
          const tier = s.labels.tier || 'unknown'
          hitsByTier[tier] = (hitsByTier[tier] || 0) + s.value
        })

        // L1 使用 hits, L2+L3 合并到 misses（因为目前只记录 L1 hits）
        const l1Hits = hitsByTier['L1'] || 0
        const l2Hits = hitsByTier['L2'] || 0
        const l3Hits = hitsByTier['L3'] || 0
        const l1Misses = missValue

        if (!cancelled) {
          setCacheData([
            { tier: 'L1', hits: Math.round(l1Hits), misses: Math.round(l1Misses) },
            { tier: 'L2', hits: Math.round(l2Hits), misses: Math.round(Math.max(0, l1Misses - l2Hits)) },
            { tier: 'L3', hits: Math.round(l3Hits), misses: Math.round(Math.max(0, l1Misses - l2Hits - l3Hits)) },
          ])
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

  const totalHits = cacheData.reduce((s, d) => s + d.hits, 0)
  const totalMisses = cacheData.reduce((s, d) => s + d.misses, 0)
  const total = totalHits + totalMisses
  const hitRate = total > 0 ? Math.round(totalHits / total * 100) : 0

  return (
    <div className="space-y-6">
      <h2 className="text-2xl font-bold">缓存监控</h2>

      {/* 汇总 */}
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4">
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
            {total > 0 ? `${Math.round(cacheData[0].hits / (cacheData[0].hits + cacheData[0].misses) * 100)}%` : '0%'} / {cacheData[0].misses} misses
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
            {total > 0 ? `${Math.round(cacheData[1].hits / (cacheData[1].hits + cacheData[1].misses || 1) * 100)}%` : '0%'} / {cacheData[1].misses} misses
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
            {total > 0 ? `${Math.round(cacheData[2].hits / (cacheData[2].hits + cacheData[2].misses || 1) * 100)}%` : '0%'} / {cacheData[2].misses} misses
          </div>
        </Card>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
        {/* 命中率对比 */}
        <Card title="各级缓存命中/未命中">
          <ResponsiveContainer width="100%" height={280}>
            <BarChart data={cacheData}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
              <XAxis dataKey="tier" tick={{ fontSize: 12, fill: 'var(--color-text-secondary)' }} />
              <YAxis tick={{ fontSize: 11, fill: 'var(--color-text-quaternary)' }} />
              <Tooltip contentStyle={{ backgroundColor: 'var(--color-bg-elevated)', border: '1px solid var(--color-border)', borderRadius: 8 }} />
              <Legend />
              <Bar dataKey="hits" fill="var(--color-success)" name="Hits" radius={[4, 4, 0, 0]} />
              <Bar dataKey="misses" fill="var(--color-danger)" name="Misses" radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </Card>

        {/* 缓存趋势 */}
        <Card title="缓存活动">
          <ResponsiveContainer width="100%" height={280}>
            <BarChart data={[
              { time: 'L1', hits: cacheData[0].hits, misses: cacheData[0].misses },
              { time: 'L2', hits: cacheData[1].hits, misses: cacheData[1].misses },
              { time: 'L3', hits: cacheData[2].hits, misses: cacheData[2].misses },
            ]}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
              <XAxis dataKey="time" tick={{ fontSize: 11, fill: 'var(--color-text-quaternary)' }} />
              <YAxis tick={{ fontSize: 11, fill: 'var(--color-text-quaternary)' }} />
              <Tooltip contentStyle={{ backgroundColor: 'var(--color-bg-elevated)', border: '1px solid var(--color-border)', borderRadius: 8 }} />
              <Legend />
              <Bar dataKey="hits" fill="var(--color-success)" name="Hits" radius={[4, 4, 0, 0]} />
              <Bar dataKey="misses" fill="var(--color-danger)" name="Misses" radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </Card>
      </div>
    </div>
  )
}
