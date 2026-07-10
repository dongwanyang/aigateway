import { useEffect, useState } from 'react'
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, PieChart, Pie, Cell, Legend } from 'recharts'
import { DollarSign, TrendingUp } from 'lucide-react'
import Card from '@/components/Card'
import { getMetricsText, parseMetrics, metricsQuery } from '@/api/client'

const CHART_COLORS = ['#3B82F6', '#10B981', '#F59E0B', '#EF4444', '#8B5CF6', '#EC4899', '#06B6D4', '#84CC16']

export default function Costs() {
  const [totalCost, setTotalCost] = useState(0)
  const [costByModel, setCostByModel] = useState<{ name: string; cost: number }[]>([])
  const [costByGroup, setCostByGroup] = useState<{ name: string; cost: number }[]>([])
  const [costHistory, setCostHistory] = useState<{ date: string; cost: number }[]>([])
  const [avgCost, setAvgCost] = useState(0)
  const [totalRequests, setTotalRequests] = useState(0)

  useEffect(() => {
    let cancelled = false

    async function load() {
      try {
        const text = await getMetricsText()
        const samples = parseMetrics(text)

        const costTotal = samples.find(s => s.name === 'gateway_cost_total')?.value ?? 0
        const modelSamples = samples.filter(s => s.name === 'gateway_cost_by_model_total')
        const groupSamples = samples.filter(s => s.name === 'gateway_cost_by_group')
        const requestSamples = samples.filter(s => s.name === 'gateway_http_requests_total')

        const totalRequestsCount = requestSamples.reduce((s, r) => s + r.value, 0)

        if (!cancelled) {
          setTotalCost(costTotal)
          setCostByModel(modelSamples.map(m => ({ name: m.labels.model || 'unknown', cost: m.value })))
          setCostByGroup(groupSamples.map(g => ({ name: g.labels.group_id || 'unknown', cost: g.value })))
          setTotalRequests(Math.round(totalRequestsCount))
          setAvgCost(totalRequestsCount > 0 ? costTotal / totalRequestsCount : 0)
        }
      } catch {
        // ignore
      }
    }

    load()
    const interval = setInterval(load, 10000)
    return () => { cancelled = true; clearInterval(interval) }
  }, [])

  // Load real cost trend from Prometheus (last 7 days, 1h buckets)
  useEffect(() => {
    let cancelled = false

    async function loadTrend() {
      try {
        const end = Math.floor(Date.now() / 1000)
        const start = end - 7 * 86400 // 7 days ago
        // increase() over a 1h window yields the cost accrued during that hour
        // (rate() would be cost/second, which understates spend by ~3600x).
        const resp = await metricsQuery({
          query: 'increase(gateway_cost_total[1h])',
          start: String(start),
          end: String(end),
          step: '3600',
        })

        const matrix = resp.data.result ?? []

        // Sum each hourly bucket's accrued cost into its calendar day
        const dayCosts: Record<string, number> = {}
        for (const item of matrix) {
          if (!item.values) continue
          for (const v of item.values) {
            const ts = parseInt(v.timestamp, 10)
            const val = parseFloat(v.value)
            if (!Number.isFinite(val)) continue
            const date = new Date(ts * 1000)
            const key = `${date.getMonth() + 1}/${date.getDate()}`
            dayCosts[key] = (dayCosts[key] || 0) + val
          }
        }

        if (!cancelled) {
          const today = new Date()
          const history: { date: string; cost: number }[] = []
          for (let i = 6; i >= 0; i--) {
            const d = new Date(today)
            d.setDate(d.getDate() - i)
            const key = `${d.getMonth() + 1}/${d.getDate()}`
            history.push({
              date: key,
              cost: Math.round((dayCosts[key] || 0) * 100) / 100,
            })
          }
          setCostHistory(history)
        }
      } catch {
        // Prometheus may not be available; fall back to empty
      }
    }

    loadTrend()
  }, [])

  const pieData = costByGroup.length > 0
    ? costByGroup
    : costByModel.length > 0
      ? costByModel
      : [{ name: '暂无数据', cost: 0 }]

  const totalModelCost = pieData.reduce((s, d) => s + d.cost, 0)
  const cacheSaved = Math.round(totalModelCost * 0.3 * 100) / 100

  return (
    <div className="space-y-6">
      <h2 className="text-2xl font-bold">成本分析</h2>

      {/* 总览卡片 */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <Card>
          <div className="flex items-center gap-3 mb-2">
            <DollarSign size={20} style={{ color: 'var(--color-primary)' }} />
            <span className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>总成本</span>
          </div>
          <div className="text-3xl font-bold">${totalCost.toFixed(4)}</div>
          <div className="text-sm mt-1" style={{ color: 'var(--color-text-tertiary)' }}>
            {totalRequests} 次请求
          </div>
        </Card>
        <Card>
          <div className="flex items-center gap-3 mb-2">
            <span className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>平均单次成本</span>
          </div>
          <div className="text-3xl font-bold">${avgCost.toFixed(6)}</div>
          <div className="text-sm mt-1" style={{ color: 'var(--color-success)' }}>
            <TrendingUp size={12} className="inline mr-1" />
            缓存节省 ~${cacheSaved.toFixed(2)}
          </div>
        </Card>
        <Card>
          <div className="flex items-center gap-3 mb-2">
            <span className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>模型数</span>
          </div>
          <div className="text-3xl font-bold">{costByModel.length}</div>
          <div className="text-sm mt-1" style={{ color: 'var(--color-text-tertiary)' }}>
            已追踪模型
          </div>
        </Card>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
        {/* 成本趋势 */}
        <Card title="成本趋势 (近7天)">
          <ResponsiveContainer width="100%" height={280}>
            <BarChart data={costHistory}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
              <XAxis dataKey="date" tick={{ fontSize: 11, fill: 'var(--color-text-quaternary)' }} />
              <YAxis tick={{ fontSize: 11, fill: 'var(--color-text-quaternary)' }} />
              <Tooltip contentStyle={{ backgroundColor: 'var(--color-bg-elevated)', border: '1px solid var(--color-border)', borderRadius: 8 }} />
              <Bar dataKey="cost" fill="var(--color-primary)" radius={[4, 4, 0, 0]} name="Cost ($)" />
            </BarChart>
          </ResponsiveContainer>
        </Card>

        {/* 按用户组分布 */}
        <Card title="成本分布 by 用户组">
          <ResponsiveContainer width="100%" height={280}>
            <PieChart>
              <Pie
                data={pieData}
                cx="50%"
                cy="50%"
                innerRadius={60}
                outerRadius={100}
                paddingAngle={2}
                dataKey="cost"
              >
                {pieData.map((_entry, index) => (
                  <Cell key={`cell-${index}`} fill={CHART_COLORS[index % CHART_COLORS.length]} />
                ))}
              </Pie>
              <Legend />
              <Tooltip contentStyle={{ backgroundColor: 'var(--color-bg-elevated)', border: '1px solid var(--color-border)', borderRadius: 8 }} />
            </PieChart>
          </ResponsiveContainer>
          <div className="grid grid-cols-2 gap-2 mt-4">
            {pieData.slice(0, 4).map((m, i) => (
              <div key={m.name} className="flex items-center gap-2">
                <div className="w-3 h-3 rounded-sm" style={{ backgroundColor: CHART_COLORS[i % CHART_COLORS.length] }} />
                <span className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
                  {m.name} — ${m.cost.toFixed(4)}
                </span>
              </div>
            ))}
          </div>
        </Card>
      </div>
    </div>
  )
}
