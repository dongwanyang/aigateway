import { useEffect, useState } from 'react'
import { Search, Filter } from 'lucide-react'
import Card from '@/components/Card'
import { getRequestLogs } from '@/api/client'
import type { LogEntry } from '@/api/client'

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

  useEffect(() => {
    getRequestLogs({ pageSize: 100 })
      .then(r => { setLogs(r.data.items); setLoading(false) })
      .catch(() => { setLoading(false) })
  }, [])

  const filtered = logs.filter(log => {
    const matchSearch = !searchTerm ||
      (log.trace_id || '').includes(searchTerm) ||
      (log.request_id || '').includes(searchTerm) ||
      (log.user_id || '').includes(searchTerm)
    const matchStatus = filterStatus === 'all' || String(log.status) === filterStatus
    const matchCache = !filterCache || log.cache_hit
    return matchSearch && matchStatus && matchCache
  })

  const formatTime = (ts: number) => {
    return new Date(ts * 1000).toLocaleString()
  }

  return (
    <div className="space-y-6">
      <h2 className="text-2xl font-bold">请求日志</h2>

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
            onChange={e => setFilterStatus(e.target.value)}
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
            onChange={e => setFilterCache(e.target.checked)}
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
                <tr><td colSpan={8} className="text-center py-8">加载中...</td></tr>
              ) : filtered.length === 0 ? (
                <tr><td colSpan={8} className="text-center py-8" style={{ color: 'var(--color-text-tertiary)' }}>暂无请求日志</td></tr>
              ) : (
                filtered.map((log, idx) => (
                  <tr key={`${log.request_id}-${idx}`}>
                    <td style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-text-tertiary)' }}>
                      {formatTime(log.timestamp)}
                    </td>
                    <td style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--font-size-xs)' }}>
                      {log.request_id.slice(0, 12)}...
                    </td>
                    <td style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--font-size-xs)' }}>
                      <span className="cursor-pointer hover:underline" style={{ color: 'var(--color-primary)' }}>
                        {log.trace_id.slice(0, 12)}...
                      </span>
                    </td>
                    <td style={{ fontSize: 'var(--font-size-sm)' }}>{log.user_id || '—'}</td>
                    <td>
                      <span className="badge badge-neutral">{log.model}</span>
                    </td>
                    <td><span className={`badge ${statusColor(log.status)}`}>{log.status}</span></td>
                    <td style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--font-size-sm)' }}>
                      {log.duration_ms}ms
                    </td>
                    <td>
                      {log.cache_hit ? (
                        <span className={`badge badge-success`}>{log.tier || 'L1'}</span>
                      ) : (
                        <span style={{ color: 'var(--color-text-quaternary)', fontSize: 'var(--font-size-sm)' }}>—</span>
                      )}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  )
}
