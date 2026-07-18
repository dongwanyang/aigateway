import { useEffect, useRef, useState } from 'react'
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, PieChart, Pie, Cell, Legend } from 'recharts'
import { DollarSign, TrendingUp } from 'lucide-react'
import Card from '@/components/Card'
import { getCostSummary, getMetricsText, parseMetrics, metricsQuery, type CostSummary } from '@/api/client'

const CHART_COLORS = ['#3B82F6', '#10B981', '#F59E0B', '#EF4444', '#8B5CF6', '#EC4899', '#06B6D4', '#84CC16']

export default function Costs() {
  const [totalCost, setTotalCost] = useState(0)
  const [costByModel, setCostByModel] = useState<{ name: string; cost: number }[]>([])
  const [costByGroup, setCostByGroup] = useState<{ name: string; cost: number }[]>([])
  const [totalRequests, setTotalRequests] = useState(0)
  const [costHistory, setCostHistory] = useState<{ date: string; cost: number }[]>([])
  const [cacheHits, setCacheHits] = useState(0)
  const [totalTokens, setTotalTokens] = useState(0)
  const [loading, setLoading] = useState(true)
  const [ledgerError, setLedgerError] = useState(false)

  // Derived from state to avoid stale closures
  const avgCost = totalRequests > 0 ? totalCost / totalRequests : 0

  // Ref 让 Prometheus 兜底 effect 读到最新值而不必把 state 放进依赖数组(否则每 15s 轮询都会重建 interval)
  const totalsRef = useRef({ cost: 0, req: 0 })
  totalsRef.current = { cost: totalCost, req: totalRequests }
  // 账本是否已提供日级趋势。Prometheus 趋势仅在账本无日级数据时兜底,
  // 否则 rebuild 后 Prometheus 计数器重置,increase() 给出 0/负值会覆盖账本正确趋势。
  const ledgerHasDaysRef = useRef(false)

  // Primary: SQLite ledger (persists across container rebuilds)
  useEffect(() => {
    let cancelled = false

    async function load() {
      try {
        // days=7: 让汇总口径与下方 7 天趋势图一致,避免"总成本"与 7 天图表口径不符
        const summary: CostSummary = await getCostSummary(7)
        if (!cancelled) {
          const t = summary.total ?? {}
          const cost = parseFloat(String(t.cost_usd ?? 0))
          const req = parseInt(String(t.requests ?? 0), 10) || 0
          const hits = parseInt(String(t.cache_hits ?? 0), 10) || 0
          const toks = parseInt(String(t.tokens_total ?? 0), 10) || 0

          setLedgerError(false)
          setTotalCost(cost)
          setTotalRequests(req)
          setCacheHits(hits)
          setTotalTokens(toks)

          setCostByModel((summary.by_model ?? []).map((r: { k: string; cost_usd: number }) => ({
            name: r.k || 'unknown', cost: parseFloat(String(r.cost_usd ?? 0)),
          })))
          setCostByGroup((summary.by_group ?? []).map((r: { k: string; cost_usd: number }) => ({
            name: r.k || 'unknown', cost: parseFloat(String(r.cost_usd ?? 0)),
          })))

          // day-level history for bar chart
          const dayRows = summary.by_day ?? []
          const dayMap: Record<string, number> = {}
          for (const d of dayRows) {
            const key = String((d as any).k ?? '')
            if (key) dayMap[key] = parseFloat(String(d.cost_usd ?? 0))
          }
          // 仅当账本有日级行时才覆盖趋势图,避免空账本每 15s 写入 7 个 0 覆盖 Prometheus 数据
          ledgerHasDaysRef.current = dayRows.length > 0
          if (dayRows.length > 0) {
            const today = new Date()
            const history: { date: string; cost: number }[] = []
            for (let i = 6; i >= 0; i--) {
              const dt = new Date(today)
              dt.setUTCDate(dt.getUTCDate() - i)
              // Backend _now_iso() is UTC; substr(ts,1,10) yields UTC YYYY-MM-DD.
              // Build keys in UTC to match, else non-UTC users see a 1-day shift.
              const key = dt.toISOString().slice(0, 10)
              history.push({ date: key, cost: Math.round((dayMap[key] ?? 0) * 100) / 100 })
            }
            setCostHistory(history)
          }
        }
      } catch {
        // 账本不可用 — 标记错误态,前端显示 Prometheus 兜底数据并提示
        if (!cancelled) setLedgerError(true)
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    load()
    const interval = setInterval(load, 15000)
    return () => { cancelled = true; clearInterval(interval) }
  }, [])

  // Fallback: Prometheus trend chart (same logic as before)
  useEffect(() => {
    let cancelled = false

    async function loadTrend() {
      try {
        const end = Math.floor(Date.now() / 1000)
        const start = end - 7 * 86400
        const resp = await metricsQuery({
          query: 'increase(gateway_cost_total[1h])',
          start: String(start),
          end: String(end),
          step: '3600',
        })

        const matrix = resp.data.result ?? []
        const dayCosts: Record<string, number> = {}
        for (const item of matrix) {
          if (!item.values) continue
          for (const v of item.values) {
            const ts = parseInt(v.timestamp, 10)
            const val = parseFloat(v.value)
            if (!Number.isFinite(val)) continue
            // Prometheus timestamps are UTC unix seconds; bucket by UTC date
            const key = new Date(ts * 1000).toISOString().slice(0, 10)
            dayCosts[key] = (dayCosts[key] || 0) + val
          }
        }

        if (!cancelled) {
          // 账本已提供日级趋势时,Prometheus 不覆盖(rebuild 后计数器重置,increase() 失真)
          if (ledgerHasDaysRef.current) return
          const today = new Date()
          const history: { date: string; cost: number }[] = []
          for (let i = 6; i >= 0; i--) {
            const dt = new Date(today)
            dt.setUTCDate(dt.getUTCDate() - i)
            const key = dt.toISOString().slice(0, 10)
            history.push({
              date: key,
              cost: Math.round((dayCosts[key] || 0) * 100) / 100,
            })
          }
          setCostHistory(history)
        }
      } catch {
        // Prometheus may not be available; keep empty or ledger-based history
      }
    }

    loadTrend()
  }, [])

  // Secondary: Prometheus totals (ledger has no running gauge)
  useEffect(() => {
    let cancelled = false

    async function loadProm() {
      try {
        const text = await getMetricsText()
        const samples = parseMetrics(text)

        const costTotal = samples.find(s => s.name === 'gateway_cost_total')?.value ?? 0
        const requestSamples = samples.filter(s => s.name === 'gateway_http_requests_total')
        const totalRequestsCount = requestSamples.reduce((s, r) => s + r.value, 0)

        if (!cancelled) {
          // Only update if ledger didn't provide data
          const t = totalsRef.current
          if (t.cost === 0 && t.req === 0) {
            setTotalCost(costTotal)
            setTotalRequests(Math.round(totalRequestsCount))
          }
        }
      } catch {
        // ignore
      }
    }

    loadProm()
    const interval = setInterval(loadProm, 30000)
    return () => { cancelled = true; clearInterval(interval) }
  }, [])

  const pieData = costByGroup.length > 0
    ? costByGroup
    : costByModel.length > 0
      ? costByModel
      : [{ name: loading ? '加载中...' : '暂无数据', cost: 0 }]

  const cacheSaved = Math.round(totalCost * 0.3 * 100) / 100

  return (
    <div className="space-y-6">
      <h2 className="text-2xl font-bold">成本分析</h2>

      {ledgerError && (
        <div className="text-sm px-3 py-2 rounded" style={{ color: 'var(--color-warning, #d97706)', backgroundColor: 'var(--color-bg-elevated)', border: '1px solid var(--color-border)' }}>
          成本账本不可用,以下为 Prometheus 兜底数据(明细/缓存命中可能缺失)。
        </div>
      )}

      {/* 总览卡片 */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <Card>
          <div className="flex items-center gap-3 mb-2">
            <DollarSign size={20} style={{ color: 'var(--color-primary)' }} />
            <span className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>总成本 (近7天)</span>
          </div>
          <div className="text-3xl font-bold">${totalCost.toFixed(4)}</div>
          <div className="text-sm mt-1" style={{ color: 'var(--color-text-tertiary)' }}>
            {totalRequests} 次请求 · 缓存命中 {cacheHits} 次
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
            已追踪模型 · {totalTokens.toLocaleString()} tokens
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
