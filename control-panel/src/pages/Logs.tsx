import { useEffect, useState, useCallback } from 'react'
import { Search, Filter, Trash2, ChevronLeft, ChevronRight, ChevronDown, ChevronUp, X } from 'lucide-react'
import Card from '@/components/Card'
import { getRequestLogs, deleteAllLogs, getTraceDetail } from '@/api/client'
import type { LogEntry, TraceDetail } from '@/api/client'

const statusColor = (status: number) => {
  if (status >= 200 && status < 300) return 'badge-success'
  if (status === 429) return 'badge-warning'
  if (status >= 400) return 'badge-danger'
  return 'badge-neutral'
}

export default function Logs() {
  const [logs, setLogs] = useState<LogEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [searchTerm, setSearchTerm] = useState('')
  const [filterStatus, setFilterStatus] = useState<string>('all')
  const [filterCache, setFilterCache] = useState<boolean>(false)
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const [expandedRow, setExpandedRow] = useState<string | null>(null)
  const [copyFeedback, setCopyFeedback] = useState<string | null>(null)
  const [traceDetail, setTraceDetail] = useState<TraceDetail | null>(null)
  const [traceLoading, setTraceLoading] = useState(false)
  const pageSize = 50

  const loadLogs = useCallback(async () => {
    setLoading(true)
    try {
      const params: Record<string, unknown> = { page, pageSize }
      if (filterStatus !== 'all') params.status = filterStatus
      if (filterCache) params.cache_only = true
      const r = await getRequestLogs(params as any)
      setLogs(r.data.items)
      setTotal(r.data.pagination.total)
    } catch {
      // ignore
    } finally {
      setLoading(false)
    }
  }, [page, filterStatus, filterCache])

  useEffect(() => { loadLogs() }, [loadLogs])

  const filtered = logs.filter(log => {
    if (!searchTerm) return true
    return (log.trace_id || '').includes(searchTerm) ||
      (log.request_id || '').includes(searchTerm) ||
      (log.user_id || '').includes(searchTerm)
  })

  const totalPages = Math.max(1, Math.ceil(total / pageSize))

  const formatTime = (ts: number) => new Date(ts * 1000).toLocaleString()

  const handleCopy = async (text: string, id: string) => {
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
  }

  const handleDeleteAll = async () => {
    if (!confirm('确定清空所有请求日志？此操作不可撤销。')) return
    try {
      await deleteAllLogs()
      setLogs([])
      setTotal(0)
      setPage(1)
    } catch {
      alert('清空日志失败')
    }
  }

  const handleTraceClick = async (traceId: string) => {
    setTraceLoading(true)
    try {
      const r = await getTraceDetail(traceId)
      setTraceDetail(r.data)
    } catch {
      // 如果后端没有 trace 数据，用当前日志中匹配的条目构建基本视图
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
          plugin_trace: primary.plugin_trace || [],
          related_requests: matched.slice(1),
        })
      }
    } finally {
      setTraceLoading(false)
    }
  }

  const toggleExpand = (requestId: string) => {
    setExpandedRow(prev => prev === requestId ? null : requestId)
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-bold">请求日志</h2>
        <button className="btn btn-danger" onClick={handleDeleteAll} style={{ padding: '8px 16px', fontSize: '12px' }}>
          <Trash2 size={14} /> 清空日志
        </button>
      </div>

      {/* 搜索和筛选 */}
      <div className="flex flex-wrap gap-3">
        <div className="relative flex-1 min-w-[240px]">
          <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2" style={{ color: 'var(--color-text-quaternary)' }} />
          <input
            className="input pl-10 w-full"
            placeholder="搜索 trace_id / request_id / user_id..."
            value={searchTerm}
            onChange={e => setSearchTerm(e.target.value)}
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
                <tr><td colSpan={9} className="text-center py-8">加载中...</td></tr>
              ) : filtered.length === 0 ? (
                <tr><td colSpan={9} className="text-center py-8" style={{ color: 'var(--color-text-tertiary)' }}>暂无请求日志</td></tr>
              ) : (
                filtered.map((log, idx) => (
                  <>
                    <tr key={`${log.request_id}-${idx}`} style={{ cursor: 'pointer' }} onClick={() => toggleExpand(`${log.request_id}-${idx}`)}>
                      <td style={{ padding: '8px' }}>
                        {expandedRow === `${log.request_id}-${idx}` ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                      </td>
                      <td style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-text-tertiary)', whiteSpace: 'nowrap' }}>
                        {formatTime(log.timestamp)}
                      </td>
                      <td style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--font-size-xs)', wordBreak: 'break-all' }}>
                        <span
                          onClick={e => { e.stopPropagation(); handleCopy(log.request_id, `req-${idx}`) }}
                          className="cursor-pointer hover:underline"
                          style={{ color: 'var(--color-text-primary)' }}
                          title="点击复制"
                        >
                          {log.request_id}
                        </span>
                        {copyFeedback === `req-${idx}` && <span className="ml-1 text-xs" style={{ color: 'var(--color-success)' }}>✓</span>}
                      </td>
                      <td style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--font-size-xs)', wordBreak: 'break-all' }}>
                        <span
                          onClick={e => { e.stopPropagation(); handleTraceClick(log.trace_id) }}
                          className="cursor-pointer hover:underline"
                          style={{ color: 'var(--color-primary)' }}
                          title="点击查看全链路追踪"
                        >
                          {log.trace_id}
                        </span>
                        {copyFeedback === `trace-${idx}` && <span className="ml-1 text-xs" style={{ color: 'var(--color-success)' }}>✓</span>}
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
                    {/* 展开的详情面板 */}
                    {expandedRow === `${log.request_id}-${idx}` && (
                      <tr key={`detail-${idx}`}>
                        <td colSpan={9} style={{ padding: 0, border: 'none' }}>
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
                  </>
                ))
              )}
            </tbody>
          </table>
        </div>

        {/* 分页控件 */}
        {total > 0 && (
          <div className="flex items-center justify-between mt-4" style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-text-tertiary)' }}>
            <span>共 {total} 条记录</span>
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
            {/* 基本信息 */}
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

            {/* 插件执行链路 */}
            <div style={{ marginBottom: 16 }}>
              <h4 className="font-semibold mb-3" style={{ fontSize: 'var(--font-size-sm)' }}>插件执行链路</h4>
              {traceDetail.plugin_trace.length === 0 ? (
                <div style={{ color: 'var(--color-text-tertiary)', fontSize: 'var(--font-size-sm)', padding: '12px 0' }}>
                  暂无插件追踪数据（旧日志可能未包含此信息）
                </div>
              ) : (
                <div style={{ position: 'relative', paddingLeft: 20 }}>
                  {/* 竖线 */}
                  <div style={{
                    position: 'absolute', left: 7, top: 8, bottom: 8,
                    width: 2, backgroundColor: 'var(--color-border)',
                  }} />
                  {traceDetail.plugin_trace.map((step, i) => (
                    <div key={i} style={{ position: 'relative', padding: '8px 0', display: 'flex', alignItems: 'center', gap: 12 }}>
                      {/* 节点圆点 */}
                      <div style={{
                        position: 'absolute', left: -16, top: '50%', transform: 'translateY(-50%)',
                        width: 12, height: 12, borderRadius: '50%',
                        backgroundColor: step.status === 'success' ? 'var(--color-success)'
                          : step.status === 'failed' ? 'var(--color-danger)'
                          : 'var(--color-text-quaternary)',
                        border: '2px solid var(--color-bg-elevated)',
                      }} />
                      <div style={{ flex: 1 }}>
                        <span className="font-medium" style={{ fontSize: 'var(--font-size-sm)' }}>{step.plugin_name}</span>
                        <span className={`badge ml-2 ${step.status === 'success' ? 'badge-success' : step.status === 'failed' ? 'badge-danger' : 'badge-neutral'}`}>
                          {step.status}
                        </span>
                      </div>
                      <span style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--font-size-xs)', color: 'var(--color-text-tertiary)' }}>
                        {step.duration_ms.toFixed(1)}ms
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* 耗时分布条 */}
            {traceDetail.plugin_trace.length > 0 && (
              <div style={{ marginBottom: 16 }}>
                <h4 className="font-semibold mb-2" style={{ fontSize: 'var(--font-size-sm)' }}>耗时分布</h4>
                <div style={{ display: 'flex', height: 24, borderRadius: 6, overflow: 'hidden', border: '1px solid var(--color-border)' }}>
                  {traceDetail.plugin_trace.filter(s => s.status !== 'skipped').map((step, i) => {
                    const totalPluginMs = traceDetail.plugin_trace.filter(s => s.status !== 'skipped').reduce((s, p) => s + p.duration_ms, 0)
                    const pct = totalPluginMs > 0 ? (step.duration_ms / totalPluginMs) * 100 : 0
                    const colors = ['#3B82F6', '#10B981', '#F59E0B', '#EF4444', '#8B5CF6', '#EC4899']
                    return (
                      <div
                        key={i}
                        title={`${step.plugin_name}: ${step.duration_ms.toFixed(1)}ms`}
                        style={{
                          width: `${Math.max(pct, 2)}%`,
                          backgroundColor: step.status === 'failed' ? '#EF4444' : colors[i % colors.length],
                          display: 'flex', alignItems: 'center', justifyContent: 'center',
                          fontSize: 'var(--font-size-xs)', color: '#fff', fontWeight: 500,
                          overflow: 'hidden', whiteSpace: 'nowrap',
                        }}
                      >
                        {pct > 15 ? step.plugin_name : ''}
                      </div>
                    )
                  })}
                </div>
              </div>
            )}

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
