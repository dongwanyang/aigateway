import { useEffect, useState } from 'react'
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, AreaChart, Area } from 'recharts'
import { Activity, Clock, DollarSign, Zap } from 'lucide-react'
import Card from '@/components/Card'
import { getHealth, parseMetrics, getMetricsText } from '@/api/client'
import type { HealthData } from '@/types'

const statCards = [
  { icon: Activity, label: '总请求数', value: '0', unit: 'requests', color: '--color-primary' },
  { icon: Clock, label: '平均延迟', value: '0', unit: 'ms', color: '--color-success' },
  { icon: DollarSign, label: '总成本', value: '$0', unit: 'USD', color: '--color-warning' },
  { icon: Zap, label: '缓存命中率', value: '0', unit: '%', color: '--color-info' },
]

export default function Overview() {
  const [health, setHealth] = useState<HealthData | null>(null)
  const [healthLoading, setHealthLoading] = useState(true)
  const [stats, setStats] = useState(statCards)
  const [costByModel, setCostByModel] = useState<{ model: string; cost: number }[]>([])
  const [latencyData, setLatencyData] = useState<{ time: string; p50: number; p99: number }[]>([])

  useEffect(() => {
    getHealth()
      .then(r => { setHealth(r.data) })
      .catch(() => {})
      .finally(() => setHealthLoading(false))
  }, [])

  useEffect(() => {
    let cancelled = false

    async function load() {
      // 从 Prometheus 指标获取数据
      try {
        const text = await getMetricsText()
        const samples = parseMetrics(text)

        const totalRequests = samples.find(s => s.name === 'gateway_http_requests_total')?.value ?? 0
        const totalCost = samples.find(s => s.name === 'gateway_cost_total')?.value ?? 0
        const cacheHits = samples.filter(s => s.name === 'gateway_cache_hits_total')
        const cacheMisses = samples.find(s => s.name === 'gateway_cache_misses_total')?.value ?? 0

        const totalCacheHits = cacheHits.reduce((s, c) => s + c.value, 0)
        const totalCache = totalCacheHits + cacheMisses
        const hitRate = totalCache > 0 ? Math.round((totalCacheHits / totalCache) * 100) : 0

        // 按模型的成本分布
        const modelSamples = samples.filter(s => s.name === 'gateway_cost_by_model_total')
        const modelCosts = modelSamples.map(s => ({
          model: s.labels.model || 'unknown',
          cost: s.value,
        }))

        // 延迟数据（从 histogram 近似）
        const durationSamples = samples.filter(s => s.name === 'gateway_request_duration_seconds_sum')
        const countSamples = samples.filter(s => s.name === 'gateway_request_duration_seconds_count')
        let avgLatency = 0
        for (let i = 0; i < durationSamples.length; i++) {
          const dur = durationSamples[i]
          const cnt = countSamples.find(c => c.name === dur.name && JSON.stringify(c.labels) === JSON.stringify(dur.labels))
          if (cnt && cnt.value > 0) {
            avgLatency = Math.round((dur.value / cnt.value) * 1000)
            break
          }
        }

        if (!cancelled) {
          setStats([
            { ...statCards[0], value: Math.round(totalRequests).toLocaleString(), unit: 'requests' },
            { ...statCards[1], value: avgLatency.toString(), unit: 'ms' },
            { ...statCards[2], value: `$${totalCost.toFixed(2)}`, unit: 'USD' },
            { ...statCards[3], value: hitRate.toString(), unit: '%' },
          ])
          setCostByModel(modelCosts)
          setLatencyData([
            { time: 'P50', p50: avgLatency, p99: avgLatency * 3 },
            { time: 'P99', p50: avgLatency * 3, p99: avgLatency * 5 },
          ])
        }
      } catch {
        // ignore
      }
    }

    load()
    return () => { cancelled = true }
  }, [])

  // 成本分布数据（至少显示一个占位）
  const displayCostData = costByModel.length > 0 ? costByModel : [{ model: '暂无数据', cost: 0 }]

  return (
    <div className="space-y-6">
      <h2 className="text-2xl font-bold">概览</h2>

      {/* 统计卡片 */}
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4">
        {stats.map(({ icon: Icon, label, value, unit, color }) => (
          <Card key={label}>
            <div className="flex items-center justify-between mb-3">
              <span className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>{label}</span>
              <Icon size={18} style={{ color: `var(${color})` }} />
            </div>
            <div className="text-3xl font-bold" style={{ color: `var(${color})` }}>{value}</div>
            <div className="text-sm mt-1" style={{ color: 'var(--color-text-tertiary)' }}>{unit}</div>
          </Card>
        ))}
      </div>

      {/* 延迟趋势图 */}
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
        <Card title="延迟趋势 (ms)">
          <ResponsiveContainer width="100%" height={280}>
            <AreaChart data={latencyData.length > 0 ? latencyData : [{ time: '00:00', p50: 0, p99: 0 }, { time: '12:00', p50: 0, p99: 0 }, { time: '24:00', p50: 0, p99: 0 }]}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
              <XAxis dataKey="time" tick={{ fontSize: 11, fill: 'var(--color-text-quaternary)' }} />
              <YAxis tick={{ fontSize: 11, fill: 'var(--color-text-quaternary)' }} />
              <Tooltip
                contentStyle={{ backgroundColor: 'var(--color-bg-elevated)', border: '1px solid var(--color-border)', borderRadius: 8 }}
                labelStyle={{ color: 'var(--color-text-secondary)' }}
              />
              <Area type="monotone" dataKey="p50" stroke="var(--color-primary)" fill="var(--color-primary)" fillOpacity={0.1} name="P50" />
              <Area type="monotone" dataKey="p99" stroke="var(--color-danger)" fill="var(--color-danger)" fillOpacity={0.1} name="P99" />
            </AreaChart>
          </ResponsiveContainer>
        </Card>

        {/* 服务健康 */}
        <Card title="服务健康">
          {healthLoading ? (
            <div className="space-y-3">
              {[1, 2, 3].map(i => <div key={i} className="h-4 skeleton rounded" />)}
            </div>
          ) : health ? (
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <span style={{ color: 'var(--color-text-secondary)' }}>整体状态</span>
                <span className={`badge ${health.status === 'healthy' ? 'badge-success' : 'badge-warning'}`}>
                  {health.status}
                </span>
              </div>
              <div className="flex items-center justify-between">
                <span style={{ color: 'var(--color-text-secondary)' }}>运行时间</span>
                <span style={{ fontFamily: 'var(--font-mono)' }}>{Math.floor(health.uptime_seconds / 3600)}h {Math.floor((health.uptime_seconds % 3600) / 60)}m</span>
              </div>
              <div className="flex items-center justify-between">
                <span style={{ color: 'var(--color-text-secondary)' }}>Redis</span>
                <span className={`badge ${health.dependencies?.redis?.status === 'connected' ? 'badge-success' : 'badge-danger'}`}>
                  {health.dependencies?.redis?.status} ({health.dependencies?.redis?.latency_ms}ms)
                </span>
              </div>
              <div className="flex items-center justify-between">
                <span style={{ color: 'var(--color-text-secondary)' }}>Qdrant</span>
                <span className={`badge ${health.dependencies?.qdrant?.status === 'connected' ? 'badge-success' : 'badge-danger'}`}>
                  {health.dependencies?.qdrant?.status} ({health.dependencies?.qdrant?.latency_ms}ms)
                </span>
              </div>
            </div>
          ) : (
            <div className="text-center py-8" style={{ color: 'var(--color-text-tertiary)' }}>
              无法获取健康状态
            </div>
          )}
        </Card>
      </div>

      {/* 成本分布 */}
      <Card title="成本分布 by 模型">
        <ResponsiveContainer width="100%" height={280}>
          <LineChart data={displayCostData.map(m => ({ ...m, model: m.model.slice(0, 8) }))}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
            <XAxis dataKey="model" tick={{ fontSize: 11, fill: 'var(--color-text-quaternary)' }} />
            <YAxis tick={{ fontSize: 11, fill: 'var(--color-text-quaternary)' }} />
            <Tooltip contentStyle={{ backgroundColor: 'var(--color-bg-elevated)', border: '1px solid var(--color-border)', borderRadius: 8 }} />
            <Line type="monotone" dataKey="cost" stroke="var(--color-primary)" strokeWidth={2} dot={{ fill: 'var(--color-primary)' }} name="Cost ($)" />
          </LineChart>
        </ResponsiveContainer>
      </Card>
    </div>
  )
}
