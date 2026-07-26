import { useQuery } from '@tanstack/react-query'
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, AreaChart, Area } from 'recharts'
import { Activity, Clock, DollarSign, Zap, TrendingDown } from 'lucide-react'
import Card from '@/components/Card'
import { getHealth, parseMetrics, getMetricsText } from '@/api/client'
import { queryKeys } from '@/query/keys'

const statCards = [
  { icon: Activity, label: '总请求数', value: '0', unit: 'requests', color: '--color-primary' },
  { icon: Clock, label: '平均延迟', value: '0', unit: 'ms', color: '--color-success' },
  { icon: DollarSign, label: '总成本', value: '$0', unit: 'USD', color: '--color-warning' },
  { icon: Zap, label: '缓存命中率', value: '0', unit: '%', color: '--color-info' },
  { icon: TrendingDown, label: 'Token 节省', value: '0', unit: 'tokens', color: '--color-success' },
]

async function loadOverviewMetrics() {
  const samples = parseMetrics(await getMetricsText())
  const sumByMetric = (name: string) =>
    samples.filter(sample => sample.name === name).reduce((sum, sample) => sum + sample.value, 0)
  const totalRequests = sumByMetric('gateway_http_requests_total')
  const totalCost = sumByMetric('gateway_cost_by_model_total')
  const totalCacheHits = sumByMetric('gateway_cache_hits_total')
  const cacheMisses = sumByMetric('gateway_cache_misses_total')
  const totalCache = totalCacheHits + cacheMisses
  const hitRate = totalCache > 0 ? Math.round((totalCacheHits / totalCache) * 100) : 0
  const tokensSaved = sumByMetric('gateway_tokens_saved_total')
  const costByUser = samples
    .filter(sample => sample.name === 'gateway_cost_by_user_total')
    .map(sample => ({ user: sample.labels.user_id || 'unknown', cost: sample.value }))
    .sort((left, right) => right.cost - left.cost)
    .slice(0, 5)

  const countSamples = samples.filter(sample => sample.name === 'gateway_request_duration_seconds_count')
  const sumSamples = samples.filter(sample => sample.name === 'gateway_request_duration_seconds_sum')
  const totalDuration = sumSamples.reduce((sum, sample) => sum + sample.value, 0)
  const totalCount = countSamples.reduce((sum, sample) => sum + sample.value, 0)
  const avgLatency = totalCount > 0 ? Math.round((totalDuration / totalCount) * 1000) : 0

  const buckets = new Map<string, Record<string, number>>()
  for (const sample of samples.filter(item => item.name === 'gateway_request_duration_seconds_bucket')) {
    const labels = { ...sample.labels }
    delete labels.le
    const key = JSON.stringify(labels)
    const entry = buckets.get(key) ?? {}
    entry[sample.labels.le ?? ''] = sample.value
    buckets.set(key, entry)
  }

  const percentile = (entry: Record<string, number>, count: number, pct: number) => {
    const limits = Object.keys(entry).filter(Boolean).sort((a, b) => Number(a) - Number(b))
    const matched = limits.find(limit => (entry[limit] ?? 0) >= (pct / 100) * count)
    const limit = matched ?? limits[limits.length - 1]
    return limit ? Math.round(Number(limit) * 1000) : 0
  }
  const first = [...buckets.entries()]
    .map(([key, entry]) => {
      const count = countSamples.find(sample => JSON.stringify(sample.labels) === key)?.value ?? 0
      return count > 0 ? {
        p50: percentile(entry, count, 50),
        p90: percentile(entry, count, 90),
        p99: percentile(entry, count, 99),
      } : null
    })
    .find(Boolean)

  const latencyData = first
    ? [
        { time: 'P50', p50: first.p50 || avgLatency, p99: first.p99 || avgLatency * 3 },
        { time: 'P90', p50: first.p90 || avgLatency * 2, p99: first.p99 || avgLatency * 4 },
        { time: 'P99', p50: first.p99 || avgLatency * 3, p99: first.p99 || avgLatency * 5 },
      ]
    : ['00:00', '08:00', '16:00', '24:00'].map(time => ({
        time,
        p50: avgLatency,
        p99: avgLatency * 3,
      }))

  return {
    stats: [
      { ...statCards[0], value: Math.round(totalRequests).toLocaleString() },
      { ...statCards[1], value: avgLatency > 0 ? String(avgLatency) : '—' },
      { ...statCards[2], value: `$${totalCost < 0.01 ? totalCost.toFixed(4) : totalCost.toFixed(2)}` },
      { ...statCards[3], value: String(hitRate) },
      { ...statCards[4], value: tokensSaved > 0 ? Math.round(tokensSaved).toLocaleString() : '0' },
    ],
    costByUser,
    latencyData,
  }
}

export default function Overview() {
  const healthQuery = useQuery({
    queryKey: queryKeys.overview.health,
    queryFn: async () => (await getHealth()).data,
    refetchInterval: 30_000,
  })
  const metricsQuery = useQuery({
    queryKey: queryKeys.overview.metrics,
    queryFn: loadOverviewMetrics,
    refetchInterval: 10_000,
  })
  const health = healthQuery.data ?? null
  const healthLoading = healthQuery.isLoading
  const stats = metricsQuery.data?.stats ?? statCards
  const costByUser = metricsQuery.data?.costByUser ?? []
  const latencyData = metricsQuery.data?.latencyData ?? []

  // 成本分布数据（至少显示一个占位）
  const displayCostData = costByUser.length > 0 ? costByUser : [{ user: '暂无数据', cost: 0 }]

  return (
    <div className="space-y-6">
      <h2 className="text-2xl font-bold">概览</h2>

      {/* 统计卡片 */}
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-5 gap-4">
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

      {/* 成本分布 by 用户 */}
      <Card title="成本分布 by 用户 (Top 5)">
        {costByUser.length === 0 ? (
          <div className="text-center py-12" style={{ color: 'var(--color-text-tertiary)' }}>
            暂无用户成本数据，等待更多请求...
          </div>
        ) : (
          <ResponsiveContainer width="100%" height={280}>
            <LineChart data={displayCostData}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
              <XAxis dataKey="user" tick={{ fontSize: 11, fill: 'var(--color-text-quaternary)' }} />
              <YAxis tick={{ fontSize: 11, fill: 'var(--color-text-quaternary)' }} />
              <Tooltip contentStyle={{ backgroundColor: 'var(--color-bg-elevated)', border: '1px solid var(--color-border)', borderRadius: 8 }} />
              <Line type="monotone" dataKey="cost" stroke="var(--color-primary)" strokeWidth={2} dot={{ fill: 'var(--color-primary)' }} name="Cost ($)" />
            </LineChart>
          </ResponsiveContainer>
        )}
      </Card>
    </div>
  )
}
