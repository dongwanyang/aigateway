import { useEffect, useState } from 'react'
import { Plus, Trash2, Eye, Search, Copy, AlertTriangle, Edit3 } from 'lucide-react'
import Card from '@/components/Card'
import {
  listApiKeys, deleteApiKey, createApiKey, updateApiKeyQuota,
  listGroups, createGroup, updateGroup, deleteGroup, assignKeyGroup,
} from '@/api/client'
import type {
  ApiKeyItem, CreateApiKeyRequest, CreateApiKeyData,
  Group, CreateGroupRequest, UpdateGroupRequest,
} from '@/types'

export default function Quotas() {
  const [keys, setKeys] = useState<ApiKeyItem[]>([])
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState('')
  const [showCreate, setShowCreate] = useState(false)
  const [justCreatedKey, setJustCreatedKey] = useState<CreateApiKeyData | null>(null)
  const [copyFeedback, setCopyFeedback] = useState<'idle' | 'copied'>('idle')
  const [createForm, setCreateForm] = useState<CreateApiKeyRequest>({
    user_id: '',
    group_id: '',
    cache_scope: 'group',
    daily_tokens: 1_000_000,
    monthly_cost: 50,
    rate_limit_rpm: 60,
    rate_limit_tpm: 100_000,
  })

  // 编辑配额状态
  const [editingKey, setEditingKey] = useState<ApiKeyItem | null>(null)
  const [editForm, setEditForm] = useState<{
    daily_tokens: number
    monthly_cost: number
    rate_limit_rpm: number
    rate_limit_tpm: number
  }>({ daily_tokens: 0, monthly_cost: 0, rate_limit_rpm: 0, rate_limit_tpm: 0 })

  // --- 用户组 Tab 状态 ---
  const [activeTab, setActiveTab] = useState<'keys' | 'groups'>('keys')
  const [groups, setGroups] = useState<Group[]>([])
  const [showCreateGroup, setShowCreateGroup] = useState(false)
  const [createGroupForm, setCreateGroupForm] = useState<CreateGroupRequest>({
    name: '', daily_tokens: 1_000_000, monthly_cost: 50, rate_limit_rpm: 60, rate_limit_tpm: 100_000,
  })
  const [editingGroup, setEditingGroup] = useState<Group | null>(null)
  const [editGroupForm, setEditGroupForm] = useState<UpdateGroupRequest & { name?: string }>({})
  const [assignKeyId, setAssignKeyId] = useState<string>('')
  const [assignGroupId, setAssignGroupId] = useState<string>('')

  const loadGroups = () => listGroups().then(r => setGroups(r.data.items)).catch(() => {})

  useEffect(() => {
    listApiKeys()
      .then(r => { setKeys(r.data.items); setLoading(false) })
      .catch(() => { setLoading(false) })
    loadGroups()
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
      const resp = await createApiKey(createForm)
      const data = resp.data
      setJustCreatedKey(data)
      setCreateForm({ user_id: '', group_id: '', cache_scope: 'group', daily_tokens: 1_000_000, monthly_cost: 50, rate_limit_rpm: 60, rate_limit_tpm: 100_000 })
      setShowCreate(false)
      const r = await listApiKeys()
      setKeys(r.data.items)
      loadGroups()
    } catch {
      alert('创建 API Key 失败')
    }
  }

  const handleCopyKey = async (fullKey: string) => {
    try {
      await navigator.clipboard.writeText(fullKey)
      setCopyFeedback('copied')
      setTimeout(() => setCopyFeedback('idle'), 2000)
    } catch {
      const ta = document.createElement('textarea')
      ta.value = fullKey
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
      setCopyFeedback('copied')
      setTimeout(() => setCopyFeedback('idle'), 2000)
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

  const handleStartEdit = (key: ApiKeyItem) => {
    setEditingKey(key)
    setEditForm({
      daily_tokens: key.quotas.daily_tokens_limit,
      monthly_cost: key.quotas.monthly_cost_limit,
      rate_limit_rpm: key.quotas.rpm_limit,
      rate_limit_tpm: key.quotas.tpm_limit,
    })
  }

  const handleSaveEdit = async () => {
    if (!editingKey) return
    try {
      await updateApiKeyQuota(editingKey.id, {
        daily_tokens: editForm.daily_tokens,
        monthly_cost: editForm.monthly_cost,
        rate_limit_rpm: editForm.rate_limit_rpm,
        rate_limit_tpm: editForm.rate_limit_tpm,
      })
      const r = await listApiKeys()
      setKeys(r.data.items)
      setEditingKey(null)
    } catch {
      alert('修改配额失败')
    }
  }

  // --- 用户组 Handlers ---
  const handleCreateGroup = async () => {
    if (!createGroupForm.name.trim()) return
    try {
      await createGroup(createGroupForm)
      setCreateGroupForm({ name: '', daily_tokens: 1_000_000, monthly_cost: 50, rate_limit_rpm: 60, rate_limit_tpm: 100_000 })
      setShowCreateGroup(false)
      loadGroups()
    } catch {
      alert('创建用户组失败')
    }
  }

  const handleStartEditGroup = (g: Group) => {
    setEditingGroup(g)
    setEditGroupForm({
      daily_tokens: g.daily_tokens_limit, monthly_cost: g.monthly_cost_limit,
      rate_limit_rpm: g.rate_limit_rpm, rate_limit_tpm: g.rate_limit_tpm, status: g.status, name: g.name,
    })
  }

  const handleSaveGroup = async () => {
    if (!editingGroup) return
    try {
      await updateGroup(editingGroup.group_id, {
        daily_tokens: editGroupForm.daily_tokens,
        monthly_cost: editGroupForm.monthly_cost,
        rate_limit_rpm: editGroupForm.rate_limit_rpm,
        rate_limit_tpm: editGroupForm.rate_limit_tpm,
        status: editGroupForm.status,
      })
      setEditingGroup(null)
      loadGroups()
    } catch {
      alert('修改用户组失败')
    }
  }

  const handleDeleteGroup = async (groupId: string) => {
    if (!confirm('确定删除该用户组?需先迁移组内所有 Key。')) return
    try {
      await deleteGroup(groupId)
      loadGroups()
    } catch {
      alert('删除用户组失败')
    }
  }

  const handleAssignKeyToGroup = async () => {
    if (!assignKeyId || !assignGroupId) return
    try {
      await assignKeyGroup(assignKeyId, assignGroupId)
      const r = await listApiKeys()
      setKeys(r.data.items)
      setAssignKeyId('')
      setAssignGroupId('')
      loadGroups()
    } catch {
      alert('分配 Key 到用户组失败')
    }
  }

  return (
    <div className="space-y-6" style={{ paddingBottom: '200px' }}>
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-bold">配额管理</h2>
        <div className="flex gap-2">
          <button
            onClick={() => setActiveTab('keys')}
            className="px-4 py-2 rounded-lg text-sm font-medium transition-colors"
            style={{
              backgroundColor: activeTab === 'keys' ? 'var(--color-primary)' : 'var(--color-bg-overlay)',
              color: activeTab === 'keys' ? 'white' : 'var(--color-text-secondary)',
              border: activeTab === 'keys' ? 'none' : '1px solid var(--color-border)',
            }}
          >
            API Keys
          </button>
          <button
            onClick={() => setActiveTab('groups')}
            className="px-4 py-2 rounded-lg text-sm font-medium transition-colors"
            style={{
              backgroundColor: activeTab === 'groups' ? 'var(--color-primary)' : 'var(--color-bg-overlay)',
              color: activeTab === 'groups' ? 'white' : 'var(--color-text-secondary)',
              border: activeTab === 'groups' ? 'none' : '1px solid var(--color-border)',
            }}
          >
            用户组
          </button>
        </div>
        {activeTab === 'keys' && (
          <button className="btn btn-primary" onClick={() => { setShowCreate(true); setJustCreatedKey(null) }}>
            <Plus size={16} /> 创建 API Key
          </button>
        )}
        {activeTab === 'groups' && (
          <button className="btn btn-primary" onClick={() => setShowCreateGroup(true)}>
            <Plus size={16} /> 创建用户组
          </button>
        )}
      </div>

      {/* ========== API Keys Tab ========== */}
      {activeTab === 'keys' && (
        <>
          {/* 刚创建成功的密钥提示 */}
          {justCreatedKey && (
            <div style={{
              background: 'rgba(245, 158, 11, 0.15)',
              border: '1px solid var(--color-warning)',
              borderRadius: '12px',
              padding: '20px 24px',
            }}>
              <div className="flex items-start gap-3">
                <AlertTriangle size={20} style={{ color: 'var(--color-warning)', flexShrink: 0, marginTop: 2 }} />
                <div className="flex-1 min-w-0">
                  <div className="font-semibold mb-1" style={{ color: 'var(--color-warning)' }}>
                    ⚠️ API Key 创建成功 — 请立刻复制保存
                  </div>
                  <div style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-text-tertiary)', marginBottom: 8 }}>
                    此密钥仅显示一次，关闭后将无法再次查看。
                  </div>
                  <div className="flex items-center gap-2">
                    <code style={{
                      fontFamily: 'var(--font-mono)',
                      fontSize: 'var(--font-size-md)',
                      background: 'var(--color-bg-elevated)',
                      padding: '8px 12px',
                      borderRadius: '6px',
                      wordBreak: 'break-all',
                      flex: 1,
                    }}>{justCreatedKey.key}</code>
                    <button
                      className="btn btn-secondary"
                      style={{ padding: '8px 12px', minWidth: 'auto', whiteSpace: 'nowrap' }}
                      onClick={() => handleCopyKey(justCreatedKey.key)}
                      title="复制到剪贴板"
                    >
                      {copyFeedback === 'copied' ? (
                        <>已复制</>
                      ) : (
                        <><Copy size={14} /> 复制</>
                      )}
                    </button>
                  </div>
                  <div className="flex gap-4 mt-2" style={{ fontSize: 'var(--font-size-xs)', color: 'var(--color-text-quaternary)' }}>
                    <span>User: {justCreatedKey.user_id}</span>
                    <span>Prefix: {justCreatedKey.key_prefix}</span>
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* 创建表单 */}
          {showCreate && !justCreatedKey && (
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
                  <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>所属用户组</label>
                  <select
                    className="input w-full"
                    value={createForm.group_id}
                    onChange={(e: React.ChangeEvent<HTMLSelectElement>) => setCreateForm((f: CreateApiKeyRequest) => ({ ...f, group_id: e.target.value }))}
                  >
                    <option value="">(无 — 使用默认组)</option>
                    {groups.map(g => (
                      <option key={g.group_id} value={g.group_id}>{g.name}</option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>缓存共享范围</label>
                  <select
                    className="input w-full"
                    value={createForm.cache_scope}
                    onChange={(e: React.ChangeEvent<HTMLSelectElement>) => setCreateForm((f: CreateApiKeyRequest) => ({ ...f, cache_scope: e.target.value as 'private' | 'group' | 'public' }))}
                  >
                    <option value="private">Private — 仅自己</option>
                    <option value="group">Group — 组内共享</option>
                    <option value="public">Public — 全局共享</option>
                  </select>
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
                <div>
                  <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>TPM 限制</label>
                  <input className="input w-full" type="number" value={createForm.rate_limit_tpm} onChange={(e: React.ChangeEvent<HTMLInputElement>) => setCreateForm((f: CreateApiKeyRequest) => ({ ...f, rate_limit_tpm: Number(e.target.value) }))} />
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

          {/* 编辑配额面板 */}
          {editingKey && (
            <Card>
              <h3 className="text-md font-semibold mb-4">修改配额 — {editingKey.user_id} ({editingKey.key_prefix}...)</h3>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>日 token 上限</label>
                  <input className="input w-full" type="number" value={editForm.daily_tokens} onChange={(e) => setEditForm(f => ({ ...f, daily_tokens: Number(e.target.value) }))} />
                </div>
                <div>
                  <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>月成本上限 ($)</label>
                  <input className="input w-full" type="number" value={editForm.monthly_cost} step={0.01} onChange={(e) => setEditForm(f => ({ ...f, monthly_cost: Number(e.target.value) }))} />
                </div>
                <div>
                  <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>RPM 限制</label>
                  <input className="input w-full" type="number" value={editForm.rate_limit_rpm} onChange={(e) => setEditForm(f => ({ ...f, rate_limit_rpm: Number(e.target.value) }))} />
                </div>
                <div>
                  <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>TPM 限制</label>
                  <input className="input w-full" type="number" value={editForm.rate_limit_tpm} onChange={(e) => setEditForm(f => ({ ...f, rate_limit_tpm: Number(e.target.value) }))} />
                </div>
              </div>
              <div className="flex gap-2 mt-4">
                <button className="btn btn-primary" onClick={handleSaveEdit}>保存</button>
                <button className="btn btn-secondary" onClick={() => setEditingKey(null)}>取消</button>
              </div>
            </Card>
          )}

          {/* 列表 */}
          <Card>
            <div className="table-container">
              <table>
                <thead>
                  <tr>
                    <th>User ID</th>
                    <th>Key 前缀</th>
                    <th>用户组</th>
                    <th>缓存范围</th>
                    <th>状态</th>
                    <th>日 Token</th>
                    <th>月成本</th>
                    <th>最后使用</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {loading ? (
                    <tr><td colSpan={9} className="text-center py-8">加载中...</td></tr>
                  ) : filtered.length === 0 ? (
                    <tr><td colSpan={9} className="text-center py-8" style={{ color: 'var(--color-text-tertiary)' }}>暂无 API Key</td></tr>
                  ) : (
                    filtered.map(key => (
                      <tr key={key.id}>
                        <td style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--font-size-sm)' }}>{key.user_id}</td>
                        <td style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--font-size-sm)' }}>{key.key_prefix}...</td>
                        <td style={{ fontSize: 'var(--font-size-sm)' }}>{key.group_name || '—'}</td>
                        <td style={{ fontSize: 'var(--font-size-sm)' }}>
                          <span className={`badge ${key.cache_scope === 'private' ? 'badge-neutral' : key.cache_scope === 'group' ? 'badge-success' : 'badge-info'}`}>
                            {key.cache_scope || '—'}
                          </span>
                        </td>
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
                            <button className="p-1.5 rounded cursor-pointer transition-colors" style={{ color: 'var(--color-text-tertiary)' }} title="修改配额" onClick={() => handleStartEdit(key)}>
                              <Edit3 size={16} />
                            </button>
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
        </>
      )}

      {/* ========== 用户组 Tab ========== */}
      {activeTab === 'groups' && (
        <>
          {/* 创建组表单 */}
          {showCreateGroup && (
            <Card>
              <h3 className="text-md font-semibold mb-4">创建新用户组</h3>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>组名称 *</label>
                  <input
                    className="input w-full"
                    placeholder="e.g. engineering"
                    value={createGroupForm.name}
                    onChange={(e: React.ChangeEvent<HTMLInputElement>) => setCreateGroupForm(f => ({ ...f, name: e.target.value }))}
                  />
                </div>
                <div>
                  <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>日 token 上限</label>
                  <input className="input w-full" type="number" value={createGroupForm.daily_tokens} onChange={(e: React.ChangeEvent<HTMLInputElement>) => setCreateGroupForm(f => ({ ...f, daily_tokens: Number(e.target.value) }))} />
                </div>
                <div>
                  <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>月成本上限 ($)</label>
                  <input className="input w-full" type="number" value={createGroupForm.monthly_cost} step={0.01} onChange={(e: React.ChangeEvent<HTMLInputElement>) => setCreateGroupForm(f => ({ ...f, monthly_cost: Number(e.target.value) }))} />
                </div>
                <div>
                  <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>RPM 限制</label>
                  <input className="input w-full" type="number" value={createGroupForm.rate_limit_rpm} onChange={(e: React.ChangeEvent<HTMLInputElement>) => setCreateGroupForm(f => ({ ...f, rate_limit_rpm: Number(e.target.value) }))} />
                </div>
                <div>
                  <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>TPM 限制</label>
                  <input className="input w-full" type="number" value={createGroupForm.rate_limit_tpm} onChange={(e: React.ChangeEvent<HTMLInputElement>) => setCreateGroupForm(f => ({ ...f, rate_limit_tpm: Number(e.target.value) }))} />
                </div>
              </div>
              <div className="flex gap-2 mt-4">
                <button className="btn btn-primary" onClick={handleCreateGroup}>创建</button>
                <button className="btn btn-secondary" onClick={() => setShowCreateGroup(false)}>取消</button>
              </div>
            </Card>
          )}

          {/* 编辑组表单 */}
          {editingGroup && (
            <Card>
              <h3 className="text-md font-semibold mb-4">编辑用户组 — {editingGroup.name}</h3>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>日 token 上限</label>
                  <input className="input w-full" type="number" value={editGroupForm.daily_tokens ?? ''} onChange={(e) => setEditGroupForm(f => ({ ...f, daily_tokens: Number(e.target.value) }))} />
                </div>
                <div>
                  <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>月成本上限 ($)</label>
                  <input className="input w-full" type="number" value={editGroupForm.monthly_cost ?? ''} step={0.01} onChange={(e) => setEditGroupForm(f => ({ ...f, monthly_cost: Number(e.target.value) }))} />
                </div>
                <div>
                  <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>RPM 限制</label>
                  <input className="input w-full" type="number" value={editGroupForm.rate_limit_rpm ?? ''} onChange={(e) => setEditGroupForm(f => ({ ...f, rate_limit_rpm: Number(e.target.value) }))} />
                </div>
                <div>
                  <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>TPM 限制</label>
                  <input className="input w-full" type="number" value={editGroupForm.rate_limit_tpm ?? ''} onChange={(e) => setEditGroupForm(f => ({ ...f, rate_limit_tpm: Number(e.target.value) }))} />
                </div>
                <div>
                  <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>状态</label>
                  <select
                    className="input w-full"
                    value={editGroupForm.status ?? 'active'}
                    onChange={(e) => setEditGroupForm(f => ({ ...f, status: e.target.value as 'active' | 'suspended' }))}
                  >
                    <option value="active">Active</option>
                    <option value="suspended">Suspended</option>
                  </select>
                </div>
              </div>
              <div className="flex gap-2 mt-4">
                <button className="btn btn-primary" onClick={handleSaveGroup}>保存</button>
                <button className="btn btn-secondary" onClick={() => setEditingGroup(null)}>取消</button>
              </div>
            </Card>
          )}

          {/* 组列表 */}
          <Card>
            <div className="table-container">
              <table>
                <thead>
                  <tr>
                    <th>组名称</th>
                    <th>成员数</th>
                    <th>日 Token</th>
                    <th>月成本</th>
                    <th>RPM</th>
                    <th>TPM</th>
                    <th>状态</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {groups.length === 0 ? (
                    <tr><td colSpan={8} className="text-center py-8" style={{ color: 'var(--color-text-tertiary)' }}>暂无用户组</td></tr>
                  ) : (
                    groups.map(g => (
                      <tr key={g.group_id}>
                        <td style={{ fontWeight: 500 }}>{g.name}</td>
                        <td>{g.member_count}</td>
                        <td>
                          <div className="flex items-center gap-2">
                            <div className="flex-1 h-2 rounded-full overflow-hidden" style={{ backgroundColor: 'var(--color-bg-overlay)' }}>
                              <div
                                className="h-full rounded-full"
                                style={{
                                  width: `${g.daily_tokens_limit > 0 ? Math.min(100, (g.daily_tokens_used / g.daily_tokens_limit) * 100) : 0}%`,
                                  backgroundColor: g.daily_tokens_used / Math.max(1, g.daily_tokens_limit) > 0.8 ? 'var(--color-danger)' : 'var(--color-success)',
                                }}
                              />
                            </div>
                            <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                              {Math.round(g.daily_tokens_used / Math.max(1, g.daily_tokens_limit) * 100)}%
                            </span>
                          </div>
                        </td>
                        <td>
                          <span style={{ color: g.monthly_cost_used / Math.max(1, g.monthly_cost_limit) > 0.8 ? 'var(--color-danger)' : 'inherit' }}>
                            ${g.monthly_cost_used.toFixed(2)} / ${g.monthly_cost_limit.toFixed(2)}
                          </span>
                        </td>
                        <td style={{ fontSize: 'var(--font-size-sm)' }}>{g.rate_limit_rpm}</td>
                        <td style={{ fontSize: 'var(--font-size-sm)' }}>{g.rate_limit_tpm}</td>
                        <td><span className={`badge ${g.status === 'active' ? 'badge-success' : 'badge-danger'}`}>{g.status}</span></td>
                        <td>
                          <div className="flex gap-1">
                            <button className="p-1.5 rounded cursor-pointer transition-colors" style={{ color: 'var(--color-text-tertiary)' }} title="编辑" onClick={() => handleStartEditGroup(g)}>
                              <Edit3 size={16} />
                            </button>
                            <button className="p-1.5 rounded cursor-pointer transition-colors" style={{ color: 'var(--color-danger)' }} title="删除" onClick={() => handleDeleteGroup(g.group_id)}>
                              <Trash2 size={16} />
                            </button>
                          </div>
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </Card>

          {/* 分配 Key 到组 */}
          <Card>
            <h3 className="text-md font-semibold mb-4">分配 API Key 到用户组</h3>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <div>
                <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>API Key</label>
                <select
                  className="input w-full"
                  value={assignKeyId}
                  onChange={(e) => setAssignKeyId(e.target.value)}
                >
                  <option value="">选择 Key</option>
                  {keys.filter(k => k.status === 'active').map(k => (
                    <option key={k.id} value={k.id}>{k.user_id} ({k.key_prefix}...)</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>目标用户组</label>
                <select
                  className="input w-full"
                  value={assignGroupId}
                  onChange={(e) => setAssignGroupId(e.target.value)}
                >
                  <option value="">选择组</option>
                  {groups.map(g => (
                    <option key={g.group_id} value={g.group_id}>{g.name}</option>
                  ))}
                </select>
              </div>
              <div className="flex items-end">
                <button className="btn btn-primary" onClick={handleAssignKeyToGroup} disabled={!assignKeyId || !assignGroupId}>
                  分配
                </button>
              </div>
            </div>
          </Card>
        </>
      )}
    </div>
  )
}
