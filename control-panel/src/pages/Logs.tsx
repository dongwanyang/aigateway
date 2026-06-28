import { useState } from 'react'
import { Search, Filter } from 'lucide-react'
import Card from '@/components/Card'

// Mock log data
const mockLogs = [
  { id: 'req_a1b2c3', trace_id: 'trace_abc123', user_id: 'dev-user-1', timestamp: '2026-06-27T14:30:00Z', model: 'gpt-4o', status: 200, duration_ms: 234, cache_hit: true, tier: 'L1' },
  { id: 'req_d4e5f6', trace_id: 'trace_def456', user_id: 'prod-service', timestamp: '2026-06-27T14:28:00Z', model: 'claude-3.5', status: 200, duration_ms: 567, cache_hit: false, tier: null },
  { id: 'req_g7h8i9', trace_id: 'trace_ghi789', user_id: 'dev-user-1', timestamp: '2026-06-27T14:25:00Z', model: 'gpt-4o', status: 429, duration_ms: 2, cache_hit: false, tier: null },
  { id: 'req_j0k1l2', trace_id: 'trace_jkl012', user_id: 'test-user', timestamp: '2026-06-27T14:20:00Z', model: 'gemini-pro', status: 200, duration_ms: 189, cache_hit: true, tier: 'L2' },
  { id: 'req_m3n4o5', trace_id: 'trace_mno345', user_id: 'prod-service', timestamp: '2026-06-27T14:15:00Z', model: 'gpt-4o-mini', status: 504, duration_ms: 5000, cache_hit: false, tier: null },
  { id: 'req_p6q7r8', trace_id: 'trace_pqr678', user_id: 'dev-user-1', timestamp: '2026-06-27T14:10:00Z', model: 'gpt-4o', status: 200, duration_ms: 312, cache_hit: true, tier: 'L3' },
]

const statusColor = (status: number) => {
  if (status >= 200 && status < 300) return 'badge-success'
  if (status === 429) return 'badge-warning'
  if (status >= 400) return 'badge-danger'
  return 'badge-neutral'
}

export default function Logs() {
  const [searchTerm, setSearchTerm] = useState('')
  const [filterStatus, setFilterStatus] = useState<string>('all')

  const filtered = mockLogs.filter(log => {
    const matchSearch = !searchTerm ||
      log.trace_id.includes(searchTerm) ||
      log.id.includes(searchTerm) ||
      log.user_id.includes(searchTerm)
    const matchStatus = filterStatus === 'all' || String(log.status) === filterStatus
    return matchSearch && matchStatus
  })

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
            <option value="504">504</option>
          </select>
        </div>
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
              {filtered.map(log => (
                <tr key={log.id}>
                  <td style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-text-tertiary)' }}>
                    {new Date(log.timestamp).toLocaleTimeString()}
                  </td>
                  <td style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--font-size-xs)' }}>
                    {log.id}
                  </td>
                  <td style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--font-size-xs)' }}>
                    <span className="cursor-pointer hover:underline" style={{ color: 'var(--color-primary)' }}>
                      {log.trace_id}
                    </span>
                  </td>
                  <td style={{ fontSize: 'var(--font-size-sm)' }}>{log.user_id}</td>
                  <td>
                    <span className="badge badge-neutral">{log.model}</span>
                  </td>
                  <td><span className={`badge ${statusColor(log.status)}`}>{log.status}</span></td>
                  <td style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--font-size-sm)' }}>
                    {log.duration_ms}ms
                  </td>
                  <td>
                    {log.cache_hit ? (
                      <span className={`badge badge-success`}>{log.tier}</span>
                    ) : (
                      <span style={{ color: 'var(--color-text-quaternary)', fontSize: 'var(--font-size-sm)' }}>—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {filtered.length === 0 && (
          <div className="empty-state">
            <div className="empty-state-title">没有找到匹配的日志</div>
            <div className="empty-state-desc">尝试调整搜索条件或筛选器</div>
          </div>
        )}
      </Card>
    </div>
  )
}
