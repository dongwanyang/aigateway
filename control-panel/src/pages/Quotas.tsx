import { useState } from 'react'
import { Plus, Trash2, Eye, Search } from 'lucide-react'
import Card from '@/components/Card'

// Mock data
const mockKeys: Array<{
  id: string
  key_prefix: string
  user_id: string
  created_at: string
  last_used_at: string | null
  status: 'active' | 'revoked' | 'suspended'
  quotas: {
    daily_tokens_used: number
    daily_tokens_limit: number
    monthly_cost_used: number
    monthly_cost_limit: number
    rpm_current: number
    rpm_limit: number
    tpm_current: number
    tpm_limit: number
  }
  usage_percentage: {
    daily_tokens: number
    monthly_cost: number
  }
}> = [
  {
    id: 'key_abc123', key_prefix: 'sk-dev-a1b2', user_id: 'dev-user-1',
    created_at: '2026-06-01T08:00:00Z', last_used_at: '2026-06-27T14:30:00Z',
    status: 'active',
    quotas: { daily_tokens_used: 50000, daily_tokens_limit: 1000000, monthly_cost_used: 2.50, monthly_cost_limit: 50.00, rpm_current: 5, rpm_limit: 60, tpm_current: 1000, tpm_limit: 100000 },
    usage_percentage: { daily_tokens: 0.05, monthly_cost: 0.05 },
  },
  {
    id: 'key_def456', key_prefix: 'sk-prod-x9y8', user_id: 'prod-service',
    created_at: '2026-05-15T10:00:00Z', last_used_at: '2026-06-27T15:00:00Z',
    status: 'active',
    quotas: { daily_tokens_used: 850000, daily_tokens_limit: 1000000, monthly_cost_used: 42.30, monthly_cost_limit: 50.00, rpm_current: 45, rpm_limit: 60, tpm_current: 85000, tpm_limit: 100000 },
    usage_percentage: { daily_tokens: 0.85, monthly_cost: 0.846 },
  },
  {
    id: 'key_ghi789', key_prefix: 'sk-test-m3n4', user_id: 'test-user',
    created_at: '2026-04-20T12:00:00Z', last_used_at: null,
    status: 'revoked',
    quotas: { daily_tokens_used: 0, daily_tokens_limit: 100000, monthly_cost_used: 0.00, monthly_cost_limit: 10.00, rpm_current: 0, rpm_limit: 30, tpm_current: 0, tpm_limit: 50000 },
    usage_percentage: { daily_tokens: 0, monthly_cost: 0 },
  },
]

export default function Quotas() {
  const [keys] = useState(mockKeys)
  const [search, setSearch] = useState('')
  const [showCreate, setShowCreate] = useState(false)

  const filtered = keys.filter(k =>
    k.user_id.includes(search.toLowerCase()) || k.key_prefix.includes(search.toLowerCase()),
  )

  const statusBadge = (status: string) => {
    switch (status) {
      case 'active': return 'badge-success'
      case 'revoked': return 'badge-neutral'
      case 'suspended': return 'badge-danger'
      default: return 'badge-neutral'
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-bold">配额管理</h2>
        <button className="btn btn-primary" onClick={() => setShowCreate(!showCreate)}>
          <Plus size={16} /> 创建 API Key
        </button>
      </div>

      {/* 创建表单 */}
      {showCreate && (
        <Card>
          <h3 className="text-md font-semibold mb-4">创建新 API Key</h3>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>用户 ID *</label>
              <input className="input w-full" placeholder="user-id" />
            </div>
            <div>
              <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>日 token 上限</label>
              <input className="input w-full" type="number" defaultValue={1000000} />
            </div>
            <div>
              <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>月成本上限 ($)</label>
              <input className="input w-full" type="number" defaultValue={50} step={0.01} />
            </div>
            <div>
              <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>RPM 限制</label>
              <input className="input w-full" type="number" defaultValue={60} />
            </div>
          </div>
          <div className="flex gap-2 mt-4">
            <button className="btn btn-primary">创建</button>
            <button className="btn btn-secondary" onClick={() => setShowCreate(false)}>取消</button>
          </div>
        </Card>
      )}

      {/* 搜索 */}
      <div className="relative">
        <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2" style={{ color: 'var(--color-text-quaternary)' }} />
        <input
          className="input pl-10 w-full"
          placeholder="搜索用户 ID 或 Key 前缀..."
          value={search}
          onChange={e => setSearch(e.target.value)}
        />
      </div>

      {/* 列表 */}
      <Card>
        <div className="table-container">
          <table>
            <thead>
              <tr>
                <th>User ID</th>
                <th>Key 前缀</th>
                <th>状态</th>
                <th>日 Token</th>
                <th>月成本</th>
                <th>最后使用</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map(key => (
                <tr key={key.id}>
                  <td style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--font-size-sm)' }}>{key.user_id}</td>
                  <td style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--font-size-sm)' }}>{key.key_prefix}...</td>
                  <td><span className={`badge ${statusBadge(key.status)}`}>{key.status}</span></td>
                  <td>
                    <div className="flex items-center gap-2">
                      <div className="flex-1 h-2 rounded-full overflow-hidden" style={{ backgroundColor: 'var(--color-bg-overlay)' }}>
                        <div
                          className="h-full rounded-full transition-all"
                          style={{
                            width: `${key.usage_percentage.daily_tokens * 100}%`,
                            backgroundColor: key.usage_percentage.daily_tokens > 0.8 ? 'var(--color-danger)' : 'var(--color-success)',
                          }}
                        />
                      </div>
                      <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                        {Math.round(key.usage_percentage.daily_tokens * 100)}%
                      </span>
                    </div>
                  </td>
                  <td>
                    <span style={{ color: key.usage_percentage.monthly_cost > 0.8 ? 'var(--color-danger)' : 'inherit' }}>
                      ${key.quotas.monthly_cost_used.toFixed(2)} / ${key.quotas.monthly_cost_limit.toFixed(2)}
                    </span>
                  </td>
                  <td style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-text-tertiary)' }}>
                    {key.last_used_at ? new Date(key.last_used_at).toLocaleDateString() : '—'}
                  </td>
                  <td>
                    <div className="flex gap-1">
                      <button className="p-1.5 rounded cursor-pointer transition-colors" style={{ color: 'var(--color-text-tertiary)' }} title="查看详情">
                        <Eye size={16} />
                      </button>
                      {key.status === 'active' && (
                        <button className="p-1.5 rounded cursor-pointer transition-colors" style={{ color: 'var(--color-danger)' }} title="撤销">
                          <Trash2 size={16} />
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  )
}
