import { useEffect, useState } from 'react'
import { Plus, Trash2, Eye, Search } from 'lucide-react'
import Card from '@/components/Card'
import { listApiKeys, deleteApiKey, createApiKey } from '@/api/client'
import type { ApiKeyItem, CreateApiKeyRequest } from '@/types'

export default function Quotas() {
  const [keys, setKeys] = useState<ApiKeyItem[]>([])
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState('')
  const [showCreate, setShowCreate] = useState(false)
  const [createForm, setCreateForm] = useState<CreateApiKeyRequest>({
    user_id: '',
    daily_tokens: 1_000_000,
    monthly_cost: 50,
    rate_limit_rpm: 60,
    rate_limit_tpm: 100_000,
  })

  useEffect(() => {
    listApiKeys()
      .then(r => { setKeys(r.data.items); setLoading(false) })
      .catch(() => { setLoading(false) })
  }, [])

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

  const handleCreate = async () => {
    if (!createForm.user_id.trim()) return
    try {
      await createApiKey(createForm)
      // Refresh list
      const r = await listApiKeys()
      setKeys(r.data.items)
      setShowCreate(false)
      setCreateForm({ user_id: '', daily_tokens: 1_000_000, monthly_cost: 50, rate_limit_rpm: 60, rate_limit_tpm: 100_000 })
    } catch {
      alert('创建 API Key 失败')
    }
  }

  const handleDelete = async (keyId: string) => {
    if (!confirm(`确定撤销 API Key ${keyId}?`)) return
    try {
      await deleteApiKey(keyId)
      setKeys(prev => prev.filter(k => k.id !== keyId))
    } catch {
      alert('撤销失败')
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
              <input
                className="input w-full"
                placeholder="user-id"
                value={createForm.user_id}
                onChange={(e: React.ChangeEvent<HTMLInputElement>) => setCreateForm((f: CreateApiKeyRequest) => ({ ...f, user_id: e.target.value }))}
              />
            </div>
            <div>
              <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>日 token 上限</label>
              <input className="input w-full" type="number" value={createForm.daily_tokens} onChange={(e: React.ChangeEvent<HTMLInputElement>) => setCreateForm((f: CreateApiKeyRequest) => ({ ...f, daily_tokens: Number(e.target.value) }))} />
            </div>
            <div>
              <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>月成本上限 ($)</label>
              <input className="input w-full" type="number" value={createForm.monthly_cost} step={0.01} onChange={(e: React.ChangeEvent<HTMLInputElement>) => setCreateForm((f: CreateApiKeyRequest) => ({ ...f, monthly_cost: Number(e.target.value) }))} />
            </div>
            <div>
              <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>RPM 限制</label>
              <input className="input w-full" type="number" value={createForm.rate_limit_rpm} onChange={(e: React.ChangeEvent<HTMLInputElement>) => setCreateForm((f: CreateApiKeyRequest) => ({ ...f, rate_limit_rpm: Number(e.target.value) }))} />
            </div>
          </div>
          <div className="flex gap-2 mt-4">
            <button className="btn btn-primary" onClick={handleCreate}>创建</button>
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
              {loading ? (
                <tr><td colSpan={7} className="text-center py-8">加载中...</td></tr>
              ) : filtered.length === 0 ? (
                <tr><td colSpan={7} className="text-center py-8" style={{ color: 'var(--color-text-tertiary)' }}>暂无 API Key</td></tr>
              ) : (
                filtered.map(key => (
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
                          <button className="p-1.5 rounded cursor-pointer transition-colors" style={{ color: 'var(--color-danger)' }} title="撤销" onClick={() => handleDelete(key.id)}>
                            <Trash2 size={16} />
                          </button>
                        )}
                      </div>
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
