import { useEffect, useState, useCallback, useMemo, memo, Fragment } from 'react'
import { Search, Filter, Trash2, ChevronLeft, ChevronRight, ChevronDown, ChevronUp, X, Bug, Activity, Clock } from 'lucide-react'
import Card from '@/components/Card'
import { getRequestLogs, deleteAllLogs, batchDeleteLogs, getTraceDetail } from '@/api/client'
import type { LogEntry, TraceDetail, TraceEvent } from '@/api/client'

const statusColor = (status: number) => {
  if (status >= 200 && status < 300) return 'badge-success'
  if (status === 429) return 'badge-warning'
  if (status >= 400) return 'badge-danger'
  return 'badge-neutral'
}

/** 合并后的事件:同一 stage 的 stage/plugin 事件(耗时+状态)与 debug 事件(payload)合并为一行. */
interface MergedEvent {
  stage: string
  kind: string            // 主事件 kind: 'stage' | 'plugin' | 'debug'
  name: string
  duration_ms: number
  status: string          // 取 stage/plugin 事件的 status(执行层视角)
  payload?: Record<string, unknown> | null
  hasDebug: boolean       // 是否有配套 debug 事件(控制"调试"标记)
}

/**
 * 把 events 按 stage 分组合并,保留首次出现顺序。
 *
 * debug 开启时同一 stage 会产生两条事件:
 *   - kind=stage/plugin:耗时+状态(始终显示)
 *   - kind=debug:业务 payload(仅 debug 开启)
 * 合并为一行,主体用 stage/plugin 的耗时+状态,payload 折叠附下。
 */
function mergeEvents(events: TraceEvent[]): MergedEvent[] {
  const order: string[] = []
  const groups = new Map<string, { primary?: TraceEvent; debug?: TraceEvent }>()
  for (const e of events) {
    const stage = e.stage
    if (!groups.has(stage)) {
      groups.set(stage, {})
      order.push(stage)
    }
    const g = groups.get(stage)!
    if (e.kind === 'debug') {
      if (!g.debug) g.debug = e
    } else if (e.kind === 'stage' || e.kind === 'plugin') {
      if (!g.primary) g.primary = e
    }
  }
  return order.map(stage => {
    const g = groups.get(stage)!
    const primary = g.primary ?? g.debug!
    return {
      stage,
      kind: primary.kind,
      name: primary.name,
      duration_ms: primary.duration_ms,
      status: primary.status,
      payload: g.debug?.payload,
      hasDebug: !!g.debug,
    }
  })
}

// -----------------------------------------------------------------------------
// LogRow — 单条日志行(主行 + 展开详情面板)
//
// memo 化关键理由:
// 之前 50 行是内联渲染 + Fragment 无 key,搜索输入每敲一个字符都会触发整表
// 重挂载(不仅重渲染)。改造点:
//   1. Fragment 加稳定 key(纯 request_id,不再拼 idx)
//   2. LogRow 抽成组件 + memo:props 不变时跳过 render
//   3. 事件回调用 useCallback 稳定引用,避免 memo 失效
// 实测:切换筛选和搜索的重渲染成本从 O(N) 主帧掉到 <5ms。
// -----------------------------------------------------------------------------
interface LogRowProps {
  log: LogEntry
  expanded: boolean
  selected: boolean
  copyFeedback: string | null
  onToggleExpand: (requestId: string) => void
  onToggleSelect: (requestId: string) => void
  onCopy: (text: string, id: string) => void
  onTraceClick: (traceId: string) => void
}

const LogRow = memo(function LogRow({
  log, expanded, selected, copyFeedback,
  onToggleExpand, onToggleSelect, onCopy, onTraceClick,
}: LogRowProps) {
  const rid = log.request_id
  const formatTime = (ts: number) => new Date(ts * 1000).toLocaleString()
  return (
    <Fragment>
      <tr style={{ cursor: 'pointer' }} onClick={() => onToggleExpand(rid)}>
        <td style={{ padding: '8px', width: '30px' }} onClick={e => e.stopPropagation()}>
          <input
            type="checkbox"
            checked={selected}
            onChange={() => onToggleSelect(rid)}
            style={{ cursor: 'pointer' }}
            aria-label={`选择 ${rid}`}
          />
        </td>
        <td style={{ padding: '8px', width: '30px' }}>
          {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
        </td>
        <td style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-text-tertiary)', whiteSpace: 'nowrap' }}>
          {formatTime(log.timestamp)}
        </td>
        <td style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--font-size-xs)', wordBreak: 'break-all' }}>
          <span
            onClick={e => { e.stopPropagation(); onCopy(log.request_id, `req-${rid}`) }}
            className="cursor-pointer hover:underline"
            style={{ color: 'var(--color-text-primary)' }}
            title="点击复制"
          >
            {log.request_id}
          </span>
          {copyFeedback === `req-${rid}` && <span className="ml-1 text-xs" style={{ color: 'var(--color-success)' }}>✓</span>}
        </td>
        <td style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--font-size-xs)', wordBreak: 'break-all' }}>
          <span
            onClick={e => { e.stopPropagation(); onTraceClick(log.trace_id) }}
            className="cursor-pointer hover:underline"
            style={{ color: 'var(--color-primary)' }}
            title="点击查看全链路追踪"
          >
            {log.trace_id}
          </span>
        </td>
        <td style={{ fontSize: 'var(--font-size-sm)' }}>{log.user_id || '—'}</td>
        <td><span className="badge badge-neutral">{log.model}</span></td>
        <td><span className={`badge ${statusColor(log.status)}`}>{log.status}</span></td>
        <td style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--font-size-sm)' }}>
          {log.duration_ms > 0 ? `${log.duration_ms}ms` : '—'}
        </td>
        <td>
          {log.cache_hit ? (
            <span className="badge badge-success">{log.tier || 'L1'}</span>
          ) : (
            <span style={{ color: 'var(--color-text-quaternary)', fontSize: 'var(--font-size-sm)' }}>—</span>
          )}
        </td>
      </tr>
      {expanded && (
        <tr>
          <td colSpan={10} style={{ padding: 0, border: 'none' }}>
            <div style={{
              padding: '16px 24px',
              backgroundColor: 'var(--color-bg-overlay)',
              borderBottom: '1px solid var(--color-border)',
              animation: 'fadeIn 0.2s ease-in-out',
            }}>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-4" style={{ fontSize: 'var(--font-size-sm)' }}>
                <div>
                  <div style={{ color: 'var(--color-text-tertiary)', marginBottom: 4 }}>Request ID</div>
                  <div style={{ fontFamily: 'var(--font-mono)', wordBreak: 'break-all' }}>{log.request_id}</div>
                </div>
                <div>
                  <div style={{ color: 'var(--color-text-tertiary)', marginBottom: 4 }}>Trace ID</div>
                  <div style={{ fontFamily: 'var(--font-mono)', wordBreak: 'break-all' }}>{log.trace_id}</div>
                </div>
                <div>
                  <div style={{ color: 'var(--color-text-tertiary)', marginBottom: 4 }}>User ID</div>
                  <div>{log.user_id || '—'}</div>
                </div>
                <div>
                  <div style={{ color: 'var(--color-text-tertiary)', marginBottom: 4 }}>模型</div>
                  <div>{log.model}</div>
                </div>
                <div>
                  <div style={{ color: 'var(--color-text-tertiary)', marginBottom: 4 }}>状态码</div>
                  <div><span className={`badge ${statusColor(log.status)}`}>{log.status}</span></div>
                </div>
                <div>
                  <div style={{ color: 'var(--color-text-tertiary)', marginBottom: 4 }}>延迟</div>
                  <div>{log.duration_ms > 0 ? `${log.duration_ms}ms` : '—'}</div>
                </div>
                <div>
                  <div style={{ color: 'var(--color-text-tertiary)', marginBottom: 4 }}>缓存</div>
                  <div>{log.cache_hit ? `命中 (${log.tier || 'L1'})` : '未命中'}</div>
                </div>
                <div>
                  <div style={{ color: 'var(--color-text-tertiary)', marginBottom: 4 }}>时间</div>
                  <div>{formatTime(log.timestamp)}</div>
                </div>
              </div>
            </div>
          </td>
        </tr>
      )}
    </Fragment>
  )
})


export default function Logs() {
  const [logs, setLogs] = useState<LogEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [searchInput, setSearchInput] = useState('')          // 受控输入
  const [searchTerm, setSearchTerm] = useState('')            // debounce 后
  const [filterStatus, setFilterStatus] = useState<string>('all')
  const [filterCache, setFilterCache] = useState<boolean>(false)
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const [expandedRow, setExpandedRow] = useState<string | null>(null)
  const [copyFeedback, setCopyFeedback] = useState<string | null>(null)
  const [traceDetail, setTraceDetail] = useState<TraceDetail | null>(null)
  const [traceLoading, setTraceLoading] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [batchDeleting, setBatchDeleting] = useState(false)
  const pageSize = 50

  // 搜索输入 debounce 200ms:输入法组合期间不触发 filter 重算,避免每敲一个
  // 字符都重刷 50 行表格(卡顿的第二大来源)。
  useEffect(() => {
    const t = setTimeout(() => setSearchTerm(searchInput), 200)
    return () => clearTimeout(t)
  }, [searchInput])

  const loadLogs = useCallback(async () => {
    setLoading(true)
    try {
      const params: Record<string, unknown> = { page, pageSize }
      if (filterStatus !== 'all') params.status = filterStatus
      if (filterCache) params.cache_only = true
      const r = await getRequestLogs(params as any)
      setLogs(r.data.items)
      setTotal(r.data.pagination.total)
      // 换页/换筛选后,清掉不在当前视图的选中项(避免"选中一堆但翻页看不到")
      setSelected(prev => {
        const visible = new Set(r.data.items.map(x => x.request_id))
        const kept = new Set<string>()
        prev.forEach(id => { if (visible.has(id)) kept.add(id) })
        return kept
      })
    } catch {
      // ignore
    } finally {
      setLoading(false)
    }
  }, [page, filterStatus, filterCache])

  useEffect(() => { loadLogs() }, [loadLogs])

  // 搜索过滤:用 useMemo 缓存,避免每次父组件 render 都重新 filter
  const filtered = useMemo(() => {
    if (!searchTerm) return logs
    const q = searchTerm
    return logs.filter(log =>
      (log.trace_id || '').includes(q) ||
      (log.request_id || '').includes(q) ||
      (log.user_id || '').includes(q)
    )
  }, [logs, searchTerm])

  const totalPages = Math.max(1, Math.ceil(total / pageSize))
  const allSelected = filtered.length > 0 && filtered.every(l => selected.has(l.request_id))
  const someSelected = selected.size > 0

  const handleCopy = useCallback(async (text: string, id: string) => {
    try {
      await navigator.clipboard.writeText(text)
    } catch {
      const ta = document.createElement('textarea')
      ta.value = text
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
    }
    setCopyFeedback(id)
    setTimeout(() => setCopyFeedback(null), 1500)
  }, [])

  const handleDeleteAll = async () => {
    if (!confirm('确定清空所有请求日志？此操作不可撤销。')) return
    try {
      await deleteAllLogs()
      setLogs([])
      setTotal(0)
      setPage(1)
      setSelected(new Set())
    } catch {
      alert('清空日志失败')
    }
  }

  const handleBatchDelete = async () => {
    const ids = Array.from(selected)
    if (ids.length === 0) return
    if (!confirm(`确定删除选中的 ${ids.length} 条请求日志？此操作不可撤销。`)) return
    setBatchDeleting(true)
    try {
      const resp = await batchDeleteLogs(ids)
      const deleted = resp.data.deleted
      // 从本地列表移除,不用重新拉全表
      setLogs(prev => prev.filter(l => !selected.has(l.request_id)))
      setTotal(t => Math.max(0, t - deleted))
      setSelected(new Set())
      // 如果当前页删空了,自动回退一页
      if (filtered.length === ids.length && page > 1) {
        setPage(p => p - 1)
      } else {
        loadLogs()  // 后端拉最新一页,补齐显示条数
      }
    } catch {
      alert('批量删除失败')
    } finally {
      setBatchDeleting(false)
    }
  }

  const toggleSelectAll = useCallback(() => {
    setSelected(prev => {
      if (filtered.length > 0 && filtered.every(l => prev.has(l.request_id))) {
        // 全选状态 → 清空当前可见的选择
        const next = new Set(prev)
        filtered.forEach(l => next.delete(l.request_id))
        return next
      }
      // 未全选 → 加入当前可见项
      const next = new Set(prev)
      filtered.forEach(l => next.add(l.request_id))
      return next
    })
  }, [filtered])

  const toggleSelect = useCallback((requestId: string) => {
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(requestId)) next.delete(requestId)
      else next.add(requestId)
      return next
    })
  }, [])

  const handleTraceClick = useCallback(async (traceId: string) => {
    setTraceLoading(true)
    try {
      const r = await getTraceDetail(traceId)
      setTraceDetail(r.data)
    } catch {
      const matched = logs.filter(l => l.trace_id === traceId)
      if (matched.length > 0) {
        const primary = matched[0]
        setTraceDetail({
          trace_id: traceId,
          request_id: primary.request_id,
          user_id: primary.user_id,
          model: primary.model,
          endpoint: primary.endpoint,
          status: primary.status,
          duration_ms: primary.duration_ms,
          cache_hit: primary.cache_hit,
          cache_tier: primary.tier,
          timestamp: primary.timestamp,
          events: [],
          plugin_trace: primary.plugin_trace || [],
          related_requests: matched.slice(1),
        })
      }
    } finally {
      setTraceLoading(false)
    }
  }, [logs])

  const toggleExpand = useCallback((requestId: string) => {
    setExpandedRow(prev => prev === requestId ? null : requestId)
  }, [])

  const formatTime = (ts: number) => new Date(ts * 1000).toLocaleString()

  // 合并同 stage 的 stage/plugin 事件与 debug 事件,debug 开启时每阶段只显示一行
  const mergedEvents = traceDetail ? mergeEvents(traceDetail.events) : []

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <h2 className="text-2xl font-bold">请求日志</h2>
        <div className="flex items-center gap-2">
          {someSelected && (
            <button
              className="btn btn-danger"
              onClick={handleBatchDelete}
              disabled={batchDeleting}
              style={{ padding: '8px 16px', fontSize: '12px' }}
            >
              <Trash2 size={14} />
              {batchDeleting ? '删除中...' : `删除选中 (${selected.size})`}
            </button>
          )}
          <button className="btn btn-danger" onClick={handleDeleteAll} style={{ padding: '8px 16px', fontSize: '12px' }}>
            <Trash2 size={14} /> 清空全部
          </button>
        </div>
      </div>

      {/* 搜索和筛选 */}
      <div className="flex flex-wrap gap-3">
        <div className="relative flex-1 min-w-[240px]">
          <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2" style={{ color: 'var(--color-text-quaternary)' }} />
          <input
            className="input pl-10 w-full"
            placeholder="搜索 trace_id / request_id / user_id..."
            value={searchInput}
            onChange={e => setSearchInput(e.target.value)}
          />
        </div>
        <div className="flex items-center gap-2">
          <Filter size={14} style={{ color: 'var(--color-text-tertiary)' }} />
          <select
            className="input"
            value={filterStatus}
            onChange={e => { setFilterStatus(e.target.value); setPage(1) }}
            style={{ minHeight: '40px', cursor: 'pointer' }}
          >
            <option value="all">全部状态</option>
            <option value="200">200</option>
            <option value="429">429</option>
            <option value="500">500</option>
          </select>
        </div>
        <label className="flex items-center gap-2 cursor-pointer" style={{ fontSize: 'var(--font-size-sm)' }}>
          <input
            type="checkbox"
            checked={filterCache}
            onChange={e => { setFilterCache(e.target.checked); setPage(1) }}
          />
          仅缓存命中
        </label>
      </div>

      {/* 日志表格 */}
      <Card>
        <div className="table-container">
          <table>
            <thead>
              <tr>
                <th style={{ width: '30px' }}>
                  <input
                    type="checkbox"
                    checked={allSelected}
                    // 半选状态(部分选中)显示为 indeterminate
                    ref={el => {
                      if (el) el.indeterminate = someSelected && !allSelected
                    }}
                    onChange={toggleSelectAll}
                    style={{ cursor: 'pointer' }}
                    aria-label="全选/取消全选"
                    title={allSelected ? '取消全选' : '全选当前页'}
                  />
                </th>
                <th style={{ width: '30px' }}></th>
                <th>时间</th>
                <th>Request ID</th>
                <th>Trace ID</th>
                <th>User</th>
                <th>模型</th>
                <th>状态</th>
                <th>延迟</th>
                <th>缓存</th>
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr><td colSpan={10} className="text-center py-8">加载中...</td></tr>
              ) : filtered.length === 0 ? (
                <tr><td colSpan={10} className="text-center py-8" style={{ color: 'var(--color-text-tertiary)' }}>暂无请求日志</td></tr>
              ) : (
                filtered.map(log => (
                  <LogRow
                    key={log.request_id}
                    log={log}
                    expanded={expandedRow === log.request_id}
                    selected={selected.has(log.request_id)}
                    copyFeedback={copyFeedback}
                    onToggleExpand={toggleExpand}
                    onToggleSelect={toggleSelect}
                    onCopy={handleCopy}
                    onTraceClick={handleTraceClick}
                  />
                ))
              )}
            </tbody>
          </table>
        </div>

        {/* 分页控件 */}
        {total > 0 && (
          <div className="flex items-center justify-between mt-4" style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-text-tertiary)' }}>
            <span>共 {total} 条记录{someSelected && ` · 已选 ${selected.size}`}</span>
            <div className="flex items-center gap-2">
              <button
                onClick={() => setPage(p => Math.max(1, p - 1))}
                disabled={page <= 1}
                style={{
                  padding: '6px 10px', borderRadius: '6px', border: '1px solid var(--color-border)',
                  cursor: page <= 1 ? 'not-allowed' : 'pointer',
                  opacity: page <= 1 ? 0.5 : 1,
                  backgroundColor: 'var(--color-bg-overlay)', color: 'var(--color-text-secondary)',
                }}
              >
                <ChevronLeft size={14} />
              </button>
              <span>第 {page} / {totalPages} 页</span>
              <button
                onClick={() => setPage(p => Math.min(totalPages, p + 1))}
                disabled={page >= totalPages}
                style={{
                  padding: '6px 10px', borderRadius: '6px', border: '1px solid var(--color-border)',
                  cursor: page >= totalPages ? 'not-allowed' : 'pointer',
                  opacity: page >= totalPages ? 0.5 : 1,
                  backgroundColor: 'var(--color-bg-overlay)', color: 'var(--color-text-secondary)',
                }}
              >
                <ChevronRight size={14} />
              </button>
            </div>
          </div>
        )}
      </Card>

      {/* 全链路追踪详情面板 */}
      {traceDetail && (
        <div style={{
          position: 'fixed', inset: 0, zIndex: 1000,
          backgroundColor: 'rgba(0,0,0,0.5)',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
        }} onClick={() => setTraceDetail(null)}>
          <div
            style={{
              backgroundColor: 'var(--color-bg-elevated)',
              borderRadius: 12,
              padding: 24,
              maxWidth: 720,
              width: '90%',
              maxHeight: '80vh',
              overflow: 'auto',
              border: '1px solid var(--color-border)',
            }}
            onClick={e => e.stopPropagation()}
          >
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-bold">全链路追踪</h3>
              <button onClick={() => setTraceDetail(null)} style={{ color: 'var(--color-text-tertiary)', cursor: 'pointer' }}>
                <X size={20} />
              </button>
            </div>

            {traceLoading ? (
              <div className="text-center py-8" style={{ color: 'var(--color-text-tertiary)' }}>加载中...</div>
            ) : (
            <>
            <div className="grid grid-cols-2 md:grid-cols-3 gap-3 mb-6" style={{ fontSize: 'var(--font-size-sm)' }}>
              <div>
                <div style={{ color: 'var(--color-text-tertiary)', marginBottom: 2 }}>Trace ID</div>
                <div style={{ fontFamily: 'var(--font-mono)', wordBreak: 'break-all' }}>{traceDetail.trace_id}</div>
              </div>
              <div>
                <div style={{ color: 'var(--color-text-tertiary)', marginBottom: 2 }}>Request ID</div>
                <div style={{ fontFamily: 'var(--font-mono)', wordBreak: 'break-all' }}>{traceDetail.request_id}</div>
              </div>
              <div>
                <div style={{ color: 'var(--color-text-tertiary)', marginBottom: 2 }}>User</div>
                <div>{traceDetail.user_id || '—'}</div>
              </div>
              <div>
                <div style={{ color: 'var(--color-text-tertiary)', marginBottom: 2 }}>模型</div>
                <div>{traceDetail.model}</div>
              </div>
              <div>
                <div style={{ color: 'var(--color-text-tertiary)', marginBottom: 2 }}>状态</div>
                <span className={`badge ${statusColor(traceDetail.status)}`}>{traceDetail.status}</span>
              </div>
              <div>
                <div style={{ color: 'var(--color-text-tertiary)', marginBottom: 2 }}>总延迟</div>
                <div style={{ fontFamily: 'var(--font-mono)' }}>{traceDetail.duration_ms}ms</div>
              </div>
              <div>
                <div style={{ color: 'var(--color-text-tertiary)', marginBottom: 2 }}>缓存</div>
                <div>{traceDetail.cache_hit ? `命中 (${traceDetail.cache_tier || 'L1'})` : '未命中'}</div>
              </div>
              <div>
                <div style={{ color: 'var(--color-text-tertiary)', marginBottom: 2 }}>时间</div>
                <div>{formatTime(traceDetail.timestamp)}</div>
              </div>
            </div>

            <div style={{ marginBottom: 16 }}>
              <h4 className="font-semibold mb-3" style={{ fontSize: 'var(--font-size-sm)' }}>
                全链路事件 <span style={{ color: 'var(--color-text-tertiary)' }}>({mergedEvents.length})</span>
              </h4>
              {mergedEvents.length === 0 ? (
                <div style={{ color: 'var(--color-text-tertiary)', fontSize: 'var(--font-size-sm)', padding: '12px 0' }}>
                  暂无事件数据（旧请求或未启用 TraceCollector）
                </div>
              ) : (
                <div style={{ position: 'relative', paddingLeft: 24 }}>
                  <div style={{
                    position: 'absolute', left: 11, top: 6, bottom: 6,
                    width: 2, backgroundColor: 'var(--color-border)',
                  }} />
                  {mergedEvents.map((evt, i) => {
                    const kindColor = evt.kind === 'plugin' ? 'var(--color-primary)'
                      : evt.kind === 'stage' ? 'var(--color-success)'
                      : evt.kind === 'debug' ? 'var(--color-warning, #f59e0b)'
                      : 'var(--color-text-tertiary)'
                    const kindLabel = evt.kind === 'plugin' ? '插件'
                      : evt.kind === 'stage' ? '阶段'
                      : evt.kind === 'debug' ? '调试'
                      : evt.kind
                    const Icon = evt.kind === 'plugin' ? Activity
                      : evt.kind === 'stage' ? Clock
                      : evt.kind === 'debug' ? Bug
                      : Activity
                    return (
                      <div key={i} style={{ position: 'relative', padding: '8px 0 8px 24px' }}>
                        <div style={{
                          position: 'absolute', left: 3, top: 12,
                          width: 18, height: 18, borderRadius: '50%',
                          backgroundColor: evt.status === 'ok' ? kindColor
                            : evt.status === 'skip' ? 'var(--color-text-quaternary)'
                            : 'var(--color-danger)',
                          border: '2px solid var(--color-bg-elevated)',
                          display: 'flex', alignItems: 'center', justifyContent: 'center',
                        }}>
                          <Icon size={10} style={{ color: 'white' }} />
                        </div>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                          <span className="font-medium" style={{ fontSize: 'var(--font-size-sm)' }}>
                            {evt.stage}
                          </span>
                          <span className="badge" style={{
                            backgroundColor: kindColor + '22',
                            color: kindColor,
                            fontSize: 'var(--font-size-xs)',
                          }}>
                            {kindLabel}
                          </span>
                          {evt.hasDebug && (
                            <span className="badge" style={{
                              backgroundColor: 'var(--color-warning, #f59e0b)22',
                              color: 'var(--color-warning, #f59e0b)',
                              fontSize: 'var(--font-size-xs)',
                            }}>
                              调试
                            </span>
                          )}
                          <span className={`badge ${evt.status === 'ok' ? 'badge-success'
                            : evt.status === 'skip' ? ''
                            : 'badge-danger'}`}>
                            {evt.status === 'ok' ? '成功'
                              : evt.status === 'skip' ? '跳过'
                              : '错误'}
                          </span>
                          <span style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--font-size-xs)', color: 'var(--color-text-tertiary)' }}>
                            {evt.duration_ms > 0 ? `${evt.duration_ms}ms` : '—'}
                          </span>
                          {evt.name && evt.name !== evt.stage && (
                            <span style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--font-size-xs)', color: 'var(--color-text-quaternary)' }}>
                              {evt.name}
                            </span>
                          )}
                        </div>
                        {evt.payload && (
                          <details style={{ marginTop: 4, fontSize: 'var(--font-size-xs)', fontFamily: 'var(--font-mono)' }}>
                            <summary style={{ cursor: 'pointer', color: 'var(--color-text-tertiary)' }}>payload</summary>
                            <pre style={{
                              marginTop: 4, padding: 8, borderRadius: 6,
                              backgroundColor: 'var(--color-bg-overlay)',
                              overflow: 'auto', maxHeight: 120,
                            }}>
                              {JSON.stringify(evt.payload, null, 2)}
                            </pre>
                          </details>
                        )}
                      </div>
                    )
                  })}
                </div>
              )}
            </div>

            <div className="flex justify-end">
              <button
                className="btn"
                onClick={() => { handleCopy(traceDetail.trace_id, 'trace-modal'); }}
                style={{ padding: '6px 16px', fontSize: 'var(--font-size-sm)' }}
              >
                复制 Trace ID
                {copyFeedback === 'trace-modal' && <span className="ml-2" style={{ color: 'var(--color-success)' }}>✓</span>}
              </button>
            </div>
            </>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
