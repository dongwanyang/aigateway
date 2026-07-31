import type { CSSProperties } from 'react'
import { useQuery } from '@tanstack/react-query'
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, AreaChart, Area } from 'recharts'
import { Activity, Clock, DollarSign, Zap, TrendingDown, Radio, ServerCog } from 'lucide-react'
import Card from '@/components/Card'
import { getHealth, getCostSummary } from '@/api/client'
import { queryKeys } from '@/query/keys'

const statCards = [
  { icon: Activity, label: '总请求数', value: '0', unit: '近 30 天 · requests', color: '--color-primary' },
  { icon: Clock, label: '平均延迟', value: '0', unit: '近 30 天 · ms', color: '--color-success' },
  { icon: DollarSign, label: '总成本', value: '$0', unit: '近 30 天 · USD', color: '--color-warning' },
  { icon: Zap, label: '缓存命中率', value: '0', unit: '近 30 天 · %', color: '--color-info' },
  { icon: TrendingDown, label: 'Token 节省', value: '0', unit: '近 30 天 · tokens', color: '--color-success' },
]

async function loadOverviewMetrics() {
  const summary = await getCostSummary(30)
  const total = summary.total ?? {}
  const totalRequests = Number(total.requests ?? 0)
  const totalCost = Number(total.cost_usd ?? 0)
  const avgLatency = Math.round(Number(total.avg_latency_ms ?? 0))
  const totalCacheHits = Number(total.cache_hits ?? 0)
  const hitRate = totalRequests > 0
    ? Math.round((totalCacheHits / totalRequests) * 100)
    : 0
  const tokensSaved = Number(total.tokens_saved ?? 0)
  const costByUser = (summary.by_user ?? [])
    .map((row: { k: string; cost_usd: number }) => ({ user: row.k || 'unknown', cost: row.cost_usd }))
    .sort((left: { cost: number }, right: { cost: number }) => right.cost - left.cost)
    .slice(0, 5)
  const latencyData = (summary.latency_by_hour ?? [])
    .filter(row => Number(row.samples ?? 0) > 0)
    .slice(-24)
    .map(row => ({
      time: row.k.slice(5, 13).replace('T', ' '),
      avg: Math.round(Number(row.avg_latency_ms ?? 0)),
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
  const metricsError = metricsQuery.error instanceof Error
    ? metricsQuery.error.message
    : metricsQuery.isError
      ? '无法加载运营指标'
      : null
  const stats = metricsQuery.data?.stats ?? statCards
  const costByUser = metricsQuery.data?.costByUser ?? []
  const latencyData = metricsQuery.data?.latencyData ?? []
  const displayCostData = costByUser.length > 0 ? costByUser : [{ user: '暂无数据', cost: 0 }]
  const healthy = health?.status === 'healthy'

  return (
    <div className="space-y-7">
      <header className="flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="page-eyebrow"><Radio size={13} /> Live operations</div>
          <h2>概览</h2>
          <p className="page-subtitle">
            汇总最近 30 天的请求、成本与缓存表现，并持续检查 Gateway 及基础设施依赖状态。
          </p>
        </div>
        <div className={`badge ${healthy ? 'badge-success' : healthLoading ? 'badge-neutral' : 'badge-warning'}`}>
          <span className="status-dot" />
          {healthLoading ? '正在检查服务状态' : healthy ? '服务运行正常' : '服务需要关注'}
        </div>
      </header>

      {metricsError && (
        <div
          role="alert"
          className="rounded-xl border px-4 py-3 text-sm"
          style={{
            borderColor: 'var(--color-danger)',
            backgroundColor: 'rgba(239, 68, 68, 0.08)',
            color: 'var(--color-danger)',
          }}
        >
          运营指标加载失败：{metricsError}。下方零值仅为占位，不代表当前没有请求。
        </div>
      )}

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-5">
        {stats.map(({ icon: Icon, label, value, unit, color }) => (
          <Card
            key={label}
            className="metric-card card-interactive"
            style={{ '--metric-color': `var(${color})` } as CSSProperties}
          >
            <div className="mb-5 flex items-start justify-between gap-3">
              <div>
                <div className="text-xs font-semibold" style={{ color: 'var(--color-text-secondary)' }}>{label}</div>
                <div className="mt-1 text-[10px]" style={{ color: 'var(--color-text-quaternary)' }}>{unit}</div>
              </div>
              <div className="metric-icon"><Icon size={18} /></div>
            </div>
            <div className="metric-value">{value}</div>
          </Card>
        ))}
      </div>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[1.35fr_0.65fr]">
        <Card title="平均延迟趋势 · 最近 24 个有请求时段 (ms)">
          <ResponsiveContainer width="100%" height={300}>
            <AreaChart data={latencyData.length > 0 ? latencyData : [{ time: '暂无数据', avg: 0 }]}>
              <defs>
                <linearGradient id="latencyGradient" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="var(--color-primary)" stopOpacity={0.28} />
                  <stop offset="100%" stopColor="var(--color-primary)" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="4 6" stroke="var(--color-border)" vertical={false} />
              <XAxis dataKey="time" tick={{ fontSize: 11, fill: 'var(--color-text-quaternary)' }} axisLine={false} tickLine={false} />
              <YAxis tick={{ fontSize: 11, fill: 'var(--color-text-quaternary)' }} axisLine={false} tickLine={false} />
              <Tooltip
                contentStyle={{ backgroundColor: 'var(--color-bg-elevated-solid)', border: '1px solid var(--color-border)', borderRadius: 12, boxShadow: 'var(--shadow-md)' }}
                labelStyle={{ color: 'var(--color-text-secondary)' }}
              />
              <Area type="monotone" dataKey="avg" stroke="var(--color-primary)" strokeWidth={2.2} fill="url(#latencyGradient)" name="平均延迟" />
            </AreaChart>
          </ResponsiveContainer>
        </Card>

        <Card title="服务健康">
          {healthLoading ? (
            <div className="space-y-4">
              {[1, 2, 3, 4].map(i => <div key={i} className="h-11 skeleton rounded-xl" />)}
            </div>
          ) : health ? (
            <div className="space-y-3">
              <div className="mb-5 flex items-center gap-3 rounded-xl border p-3" style={{ borderColor: 'var(--color-border)', background: 'var(--color-bg-overlay)' }}>
                <div className="metric-icon"><ServerCog size={18} /></div>
                <div>
                  <div className="text-xs font-semibold">Gateway Runtime</div>
                  <div className="mt-1 text-[10px]" style={{ color: 'var(--color-text-tertiary)' }}>
                    已运行 {Math.floor(health.uptime_seconds / 3600)}h {Math.floor((health.uptime_seconds % 3600) / 60)}m
                  </div>
                </div>
              </div>
              <HealthRow label="整体状态" value={health.status} state={healthy ? 'success' : 'warning'} />
              <HealthRow
                label="Redis Stack"
                value={`${health.dependencies?.redis?.status ?? 'unknown'} · ${health.dependencies?.redis?.latency_ms ?? '—'}ms`}
                state={health.dependencies?.redis?.status === 'connected' ? 'success' : 'danger'}
              />
              <HealthRow
                label="Qdrant"
                value={`${health.dependencies?.qdrant?.status ?? 'unknown'} · ${health.dependencies?.qdrant?.latency_ms ?? '—'}ms`}
                state={health.dependencies?.qdrant?.status === 'connected' ? 'success' : 'danger'}
              />
            </div>
          ) : (
            <div className="empty-state min-h-[240px] py-8">
              <ServerCog className="empty-state-icon" />
              <div className="empty-state-title">无法获取健康状态</div>
              <div className="empty-state-desc">请检查 Gateway 服务和浏览器网络连接。</div>
            </div>
          )}
        </Card>
      </div>

      <Card title="成本分布 by 用户 · 近 30 天 (Top 5)">
        {costByUser.length === 0 ? (
          <div className="empty-state min-h-[250px] py-8">
            <DollarSign className="empty-state-icon" />
            <div className="empty-state-title">暂无用户成本数据</div>
            <div className="empty-state-desc">产生请求后，这里会显示成本最高的前 5 个用户。</div>
          </div>
        ) : (
          <ResponsiveContainer width="100%" height={290}>
            <LineChart data={displayCostData}>
              <CartesianGrid strokeDasharray="4 6" stroke="var(--color-border)" vertical={false} />
              <XAxis dataKey="user" tick={{ fontSize: 11, fill: 'var(--color-text-quaternary)' }} axisLine={false} tickLine={false} />
              <YAxis tick={{ fontSize: 11, fill: 'var(--color-text-quaternary)' }} axisLine={false} tickLine={false} />
              <Tooltip contentStyle={{ backgroundColor: 'var(--color-bg-elevated-solid)', border: '1px solid var(--color-border)', borderRadius: 12, boxShadow: 'var(--shadow-md)' }} />
              <Line type="monotone" dataKey="cost" stroke="var(--color-primary)" strokeWidth={2.4} dot={{ fill: 'var(--color-primary)', strokeWidth: 0, r: 4 }} activeDot={{ r: 6 }} name="Cost ($)" />
            </LineChart>
          </ResponsiveContainer>
        )}
      </Card>
    </div>
  )
}

function HealthRow({ label, value, state }: { label: string; value: string; state: 'success' | 'warning' | 'danger' }) {
  return (
    <div className="flex items-center justify-between gap-3 border-b py-3 last:border-b-0" style={{ borderColor: 'var(--color-border-light)' }}>
      <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>{label}</span>
      <span className={`badge badge-${state}`}>{value}</span>
    </div>
  )
}
