import { useEffect, useState } from 'react'
import { Bot, Plus, Trash2, Save, RefreshCw, ChevronDown, ChevronRight, Wifi, List } from 'lucide-react'
import Card from '@/components/Card'
import { getFullConfig, updateFullConfig, testProviderConnectivity, fetchProviderModels } from '@/api/client'

// --- 类型定义 ---

interface PricingConfig {
  prompt: number
  completion: number
}

interface ModelGroup {
  models: string[]
  fallback_models: string[]
  pricing: Record<string, PricingConfig>
}

interface ProviderConfig {
  api_key: string
  base_url?: string
  model_grouper: ModelGroup[]
  num_retries: number
  retry_after: number
}

interface EmbeddingConfig {
  backend: string
  model: string
  vector_dim: number
  openai_model: string
}

// --- 组件 ---

export default function Models() {
  const [providers, setProviders] = useState<Record<string, ProviderConfig>>({})
  const [embedding, setEmbedding] = useState<EmbeddingConfig>({
    backend: 'sentence_transformers',
    model: '',
    vector_dim: 1024,
    openai_model: 'text-embedding-3-small',
  })
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [expandedProviders, setExpandedProviders] = useState<Set<string>>(new Set())
  const [newProviderName, setNewProviderName] = useState('')
  const [showAddProvider, setShowAddProvider] = useState(false)
  const [fullConfig, setFullConfig] = useState<Record<string, unknown>>({})
  const [testResults, setTestResults] = useState<Record<string, { success: boolean; latency_ms: number; error?: string; loading: boolean }>>({})
  const [fetchedModels, setFetchedModels] = useState<Record<string, { models: string[]; loading: boolean; error?: string }>>({})

  useEffect(() => {
    loadConfig()
  }, [])

  async function loadConfig() {
    setLoading(true)
    setError(null)
    try {
      const r = await getFullConfig()
      const data = r.data as Record<string, unknown>
      setFullConfig(data)

      // 解析 providers
      const rawProviders = (data.providers ?? {}) as Record<string, any>
      const parsed: Record<string, ProviderConfig> = {}
      for (const [name, cfg] of Object.entries(rawProviders)) {
        if (typeof cfg === 'object' && cfg !== null) {
          parsed[name] = {
            api_key: cfg.api_key ?? '',
            base_url: cfg.base_url ?? '',
            model_grouper: Array.isArray(cfg.model_grouper) ? cfg.model_grouper : [],
            num_retries: cfg.num_retries ?? 3,
            retry_after: cfg.retry_after ?? 1000,
          }
        }
      }
      setProviders(parsed)
      setExpandedProviders(new Set(Object.keys(parsed)))

      // 解析 embedding
      const rawEmbed = (data.embedding ?? {}) as any
      setEmbedding({
        backend: rawEmbed.backend ?? 'sentence_transformers',
        model: rawEmbed.model ?? '',
        vector_dim: rawEmbed.vector_dim ?? 1024,
        openai_model: rawEmbed.openai_model ?? 'text-embedding-3-small',
      })
    } catch (e: any) {
      setError(e.message || '加载配置失败')
    } finally {
      setLoading(false)
    }
  }

  async function handleSave() {
    setSaving(true)
    setError(null)
    setSuccess(null)
    try {
      // 构建更新的配置
      const updatedConfig = {
        ...fullConfig,
        providers,
        embedding,
      }
      await updateFullConfig(updatedConfig)
      setSuccess('模型配置已保存并生效')
      setFullConfig(updatedConfig)
      setTimeout(() => setSuccess(null), 3000)
    } catch (e: any) {
      setError(e.message || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  function toggleProviderExpand(name: string) {
    setExpandedProviders(prev => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }

  function updateProvider(name: string, field: keyof ProviderConfig, value: any) {
    setProviders(prev => ({
      ...prev,
      [name]: { ...prev[name], [field]: value },
    }))
  }

  function updateModelInGroup(providerName: string, groupIdx: number, modelIdx: number, value: string) {
    setProviders(prev => {
      const p = { ...prev[providerName] }
      const groups = [...p.model_grouper]
      const group = { ...groups[groupIdx] }
      const models = [...group.models]
      models[modelIdx] = value
      group.models = models
      groups[groupIdx] = group
      p.model_grouper = groups
      return { ...prev, [providerName]: p }
    })
  }

  function addModelToGroup(providerName: string, groupIdx: number) {
    setProviders(prev => {
      const p = { ...prev[providerName] }
      const groups = [...p.model_grouper]
      const group = { ...groups[groupIdx] }
      group.models = [...group.models, '']
      groups[groupIdx] = group
      p.model_grouper = groups
      return { ...prev, [providerName]: p }
    })
  }

  function removeModelFromGroup(providerName: string, groupIdx: number, modelIdx: number) {
    setProviders(prev => {
      const p = { ...prev[providerName] }
      const groups = [...p.model_grouper]
      const group = { ...groups[groupIdx] }
      const modelName = group.models[modelIdx]
      group.models = group.models.filter((_, i) => i !== modelIdx)
      // 同时删除对应的 pricing
      const pricing = { ...group.pricing }
      delete pricing[modelName]
      group.pricing = pricing
      groups[groupIdx] = group
      p.model_grouper = groups
      return { ...prev, [providerName]: p }
    })
  }

  function updateFallbackInGroup(providerName: string, groupIdx: number, fbIdx: number, value: string) {
    setProviders(prev => {
      const p = { ...prev[providerName] }
      const groups = [...p.model_grouper]
      const group = { ...groups[groupIdx] }
      const fallbacks = [...group.fallback_models]
      fallbacks[fbIdx] = value
      group.fallback_models = fallbacks
      groups[groupIdx] = group
      p.model_grouper = groups
      return { ...prev, [providerName]: p }
    })
  }

  function addFallbackToGroup(providerName: string, groupIdx: number) {
    setProviders(prev => {
      const p = { ...prev[providerName] }
      const groups = [...p.model_grouper]
      const group = { ...groups[groupIdx] }
      group.fallback_models = [...group.fallback_models, '']
      groups[groupIdx] = group
      p.model_grouper = groups
      return { ...prev, [providerName]: p }
    })
  }

  function removeFallbackFromGroup(providerName: string, groupIdx: number, fbIdx: number) {
    setProviders(prev => {
      const p = { ...prev[providerName] }
      const groups = [...p.model_grouper]
      const group = { ...groups[groupIdx] }
      group.fallback_models = group.fallback_models.filter((_, i) => i !== fbIdx)
      groups[groupIdx] = group
      p.model_grouper = groups
      return { ...prev, [providerName]: p }
    })
  }

  function updatePricing(providerName: string, groupIdx: number, modelName: string, field: 'prompt' | 'completion', value: string) {
    setProviders(prev => {
      const p = { ...prev[providerName] }
      const groups = [...p.model_grouper]
      const group = { ...groups[groupIdx] }
      const pricing = { ...group.pricing }
      const numVal = value === '' ? 0 : parseFloat(value)
      pricing[modelName] = {
        ...(pricing[modelName] ?? { prompt: 0, completion: 0 }),
        [field]: isNaN(numVal) ? 0 : numVal,
      }
      group.pricing = pricing
      groups[groupIdx] = group
      p.model_grouper = groups
      return { ...prev, [providerName]: p }
    })
  }

  /** 将价格数字格式化为易读的小数形式，避免科学计数法 */
  function formatPrice(value: number | undefined): string {
    if (value === undefined || value === 0) return ''
    // 使用 toFixed 确保不显示科学计数法
    // 找到有效精度
    const str = value.toFixed(10).replace(/0+$/, '').replace(/\.$/, '')
    return str
  }

  function addProvider() {
    const name = newProviderName.trim().toLowerCase()
    if (!name || providers[name]) return
    setProviders(prev => ({
      ...prev,
      [name]: {
        api_key: '',
        base_url: '',
        model_grouper: [{ models: [], fallback_models: [], pricing: {} }],
        num_retries: 3,
        retry_after: 1000,
      },
    }))
    setExpandedProviders(prev => new Set([...prev, name]))
    setNewProviderName('')
    setShowAddProvider(false)
  }

  async function handleTestConnectivity(providerName: string) {
    setTestResults(prev => ({ ...prev, [providerName]: { success: false, latency_ms: 0, loading: true } }))
    try {
      const r = await testProviderConnectivity(providerName)
      setTestResults(prev => ({ ...prev, [providerName]: { ...r.data, loading: false } }))
    } catch (e: any) {
      setTestResults(prev => ({ ...prev, [providerName]: { success: false, latency_ms: 0, error: e.message, loading: false } }))
    }
  }

  async function handleFetchModels(providerName: string) {
    setFetchedModels(prev => ({ ...prev, [providerName]: { models: [], loading: true } }))
    try {
      const r = await fetchProviderModels(providerName)
      setFetchedModels(prev => ({ ...prev, [providerName]: { models: r.data.models, loading: false } }))
    } catch (e: any) {
      setFetchedModels(prev => ({ ...prev, [providerName]: { models: [], loading: false, error: e.message } }))
    }
  }

  function removeProvider(name: string) {
    if (!confirm(`确定删除提供商 "${name}"？`)) return
    setProviders(prev => {
      const next = { ...prev }
      delete next[name]
      return next
    })
  }

  if (loading) {
    return (
      <div className="space-y-6">
        <h2 className="text-2xl font-bold">模型配置</h2>
        <div className="space-y-3">
          {[1, 2, 3].map(i => <div key={i} className="h-20 skeleton rounded" />)}
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-bold">模型配置</h2>
        <div className="flex items-center gap-2">
          <button
            className="btn btn-secondary"
            style={{ padding: '8px 14px', fontSize: '12px' }}
            onClick={loadConfig}
          >
            <RefreshCw size={14} /> 重新加载
          </button>
          <button
            className="btn btn-primary"
            style={{ padding: '8px 14px', fontSize: '12px' }}
            onClick={handleSave}
            disabled={saving}
          >
            <Save size={14} /> {saving ? '保存中...' : '保存配置'}
          </button>
        </div>
      </div>

      {error && (
        <div style={{
          padding: '10px 16px',
          borderRadius: '8px',
          backgroundColor: 'rgba(239, 68, 68, 0.1)',
          border: '1px solid var(--color-danger)',
          fontSize: '13px',
          color: 'var(--color-danger)',
        }}>
          ❌ {error}
        </div>
      )}

      {success && (
        <div style={{
          padding: '10px 16px',
          borderRadius: '8px',
          backgroundColor: 'rgba(16, 185, 129, 0.1)',
          border: '1px solid var(--color-success)',
          fontSize: '13px',
          color: 'var(--color-success)',
        }}>
          ✅ {success}
        </div>
      )}

      {/* --- Providers 区域 --- */}
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <h3 className="text-lg font-semibold">模型提供商</h3>
          <button
            className="btn btn-secondary"
            style={{ padding: '6px 12px', fontSize: '12px' }}
            onClick={() => setShowAddProvider(true)}
          >
            <Plus size={14} /> 添加提供商
          </button>
        </div>

        {showAddProvider && (
          <Card>
            <div className="flex items-center gap-3">
              <input
                className="input flex-1"
                placeholder="提供商名称 (如: openai, anthropic, deepseek...)"
                value={newProviderName}
                onChange={e => setNewProviderName(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') addProvider() }}
                autoFocus
              />
              <button className="btn btn-primary" style={{ padding: '8px 16px', fontSize: '12px' }} onClick={addProvider} disabled={!newProviderName.trim()}>
                确认添加
              </button>
              <button className="btn btn-secondary" style={{ padding: '8px 16px', fontSize: '12px' }} onClick={() => setShowAddProvider(false)}>
                取消
              </button>
            </div>
          </Card>
        )}

        {Object.entries(providers).map(([providerName, config]) => (
          <Card key={providerName}>
            {/* Provider 头部 */}
            <div
              className="flex items-center justify-between cursor-pointer"
              onClick={() => toggleProviderExpand(providerName)}
            >
              <div className="flex items-center gap-3">
                {expandedProviders.has(providerName) ? <ChevronDown size={18} /> : <ChevronRight size={18} />}
                <Bot size={20} style={{ color: 'var(--color-primary)' }} />
                <span className="font-semibold text-base">{providerName}</span>
                <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                  {config.model_grouper.flatMap(g => g.models).length} 个模型
                </span>
                {/* 连通性测试结果指示 */}
                {testResults[providerName] && !testResults[providerName].loading && (
                  <span className="text-xs px-2 py-0.5 rounded" style={{
                    backgroundColor: testResults[providerName].success ? 'rgba(16, 185, 129, 0.1)' : 'rgba(239, 68, 68, 0.1)',
                    color: testResults[providerName].success ? 'var(--color-success)' : 'var(--color-danger)',
                  }}>
                    {testResults[providerName].success ? `✓ ${testResults[providerName].latency_ms}ms` : '✗ 不可达'}
                  </span>
                )}
              </div>
              <div className="flex items-center gap-2" onClick={e => e.stopPropagation()}>
                <button
                  className="p-1.5 rounded cursor-pointer"
                  style={{ color: 'var(--color-primary)', border: '1px solid var(--color-border)' }}
                  onClick={() => handleTestConnectivity(providerName)}
                  title="测试连通性"
                  disabled={testResults[providerName]?.loading}
                >
                  {testResults[providerName]?.loading ? <RefreshCw size={14} className="animate-spin" /> : <Wifi size={14} />}
                </button>
                <button
                  className="p-1.5 rounded cursor-pointer"
                  style={{ color: 'var(--color-primary)', border: '1px solid var(--color-border)' }}
                  onClick={() => handleFetchModels(providerName)}
                  title="获取模型列表"
                  disabled={fetchedModels[providerName]?.loading}
                >
                  {fetchedModels[providerName]?.loading ? <RefreshCw size={14} className="animate-spin" /> : <List size={14} />}
                </button>
                <button
                  className="p-1.5 rounded cursor-pointer"
                  style={{ color: 'var(--color-danger)' }}
                  onClick={() => removeProvider(providerName)}
                  title="删除提供商"
                >
                  <Trash2 size={16} />
                </button>
              </div>
            </div>

            {/* Provider 展开内容 */}
            {expandedProviders.has(providerName) && (
              <div className="mt-4 space-y-4 pl-8">
                {/* 连通性测试错误 */}
                {testResults[providerName] && !testResults[providerName].loading && !testResults[providerName].success && testResults[providerName].error && (
                  <div className="p-3 rounded-lg text-xs" style={{ backgroundColor: 'rgba(239, 68, 68, 0.08)', color: 'var(--color-danger)' }}>
                    ❌ 连接失败: {testResults[providerName].error}
                  </div>
                )}

                {/* 远程获取的模型列表 */}
                {fetchedModels[providerName] && !fetchedModels[providerName].loading && (
                  <div className="p-3 rounded-lg" style={{ border: '1px solid var(--color-border)', backgroundColor: 'var(--color-bg-elevated)' }}>
                    {fetchedModels[providerName].error ? (
                      <div className="text-xs" style={{ color: 'var(--color-danger)' }}>
                        ❌ 获取模型列表失败: {fetchedModels[providerName].error}
                      </div>
                    ) : (
                      <div>
                        <div className="flex items-center justify-between mb-2">
                          <span className="text-xs font-semibold" style={{ color: 'var(--color-text-secondary)' }}>
                            远程可用模型 ({fetchedModels[providerName].models.length})
                          </span>
                          <button
                            className="text-xs cursor-pointer px-2 py-0.5 rounded"
                            style={{ color: 'var(--color-text-secondary)', border: '1px solid var(--color-border)' }}
                            onClick={() => setFetchedModels(prev => { const next = { ...prev }; delete next[providerName]; return next })}
                          >
                            关闭
                          </button>
                        </div>
                        <div className="flex flex-wrap gap-2 max-h-40 overflow-y-auto p-1">
                          {fetchedModels[providerName].models.map(m => (
                            <span
                              key={m}
                              className="text-xs px-2.5 py-1 rounded-md cursor-pointer transition-colors"
                              style={{
                                backgroundColor: 'var(--color-bg-overlay)',
                                border: '1px solid var(--color-border)',
                                color: 'var(--color-text-primary)',
                              }}
                              title="点击添加到模型列表"
                              onClick={() => {
                                const group = config.model_grouper[0]
                                if (group && !group.models.includes(m)) {
                                  addModelToGroup(providerName, 0)
                                  updateModelInGroup(providerName, 0, group.models.length, m)
                                }
                              }}
                              onMouseEnter={e => {
                                (e.target as HTMLElement).style.backgroundColor = 'var(--color-primary)'
                                ;(e.target as HTMLElement).style.color = 'white'
                                ;(e.target as HTMLElement).style.borderColor = 'var(--color-primary)'
                              }}
                              onMouseLeave={e => {
                                (e.target as HTMLElement).style.backgroundColor = 'var(--color-bg-overlay)'
                                ;(e.target as HTMLElement).style.color = 'var(--color-text-primary)'
                                ;(e.target as HTMLElement).style.borderColor = 'var(--color-border)'
                              }}
                            >
                              {m}
                            </span>
                          ))}
                        </div>
                        <div className="text-xs mt-2" style={{ color: 'var(--color-text-quaternary)' }}>
                          💡 点击模型名称可添加到配置中
                        </div>
                      </div>
                    )}
                  </div>
                )}

                {/* 基础配置 */}
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  <div>
                    <label className="block text-xs mb-1 font-medium" style={{ color: 'var(--color-text-tertiary)' }}>API Key</label>
                    <input
                      className="input w-full"
                      type="password"
                      placeholder="sk-..."
                      value={config.api_key}
                      onChange={e => updateProvider(providerName, 'api_key', e.target.value)}
                    />
                  </div>
                  <div>
                    <label className="block text-xs mb-1 font-medium" style={{ color: 'var(--color-text-tertiary)' }}>Base URL (可选)</label>
                    <input
                      className="input w-full"
                      placeholder="https://api.openai.com/v1"
                      value={config.base_url ?? ''}
                      onChange={e => updateProvider(providerName, 'base_url', e.target.value)}
                    />
                  </div>
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  <div>
                    <label className="block text-xs mb-1 font-medium" style={{ color: 'var(--color-text-tertiary)' }}>重试次数</label>
                    <input
                      className="input w-full"
                      type="number"
                      min={0}
                      max={10}
                      value={config.num_retries}
                      onChange={e => updateProvider(providerName, 'num_retries', parseInt(e.target.value) || 0)}
                    />
                  </div>
                  <div>
                    <label className="block text-xs mb-1 font-medium" style={{ color: 'var(--color-text-tertiary)' }}>重试间隔 (ms)</label>
                    <input
                      className="input w-full"
                      type="number"
                      min={0}
                      value={config.retry_after}
                      onChange={e => updateProvider(providerName, 'retry_after', parseInt(e.target.value) || 0)}
                    />
                  </div>
                </div>

                {/* 模型组 */}
                {config.model_grouper.map((group, gIdx) => (
                  <div key={gIdx} className="p-4 rounded-lg" style={{ border: '1px solid var(--color-border)', backgroundColor: 'var(--color-bg-overlay)' }}>
                    <div className="space-y-3">
                      {/* 主模型列表 */}
                      <div>
                        <div className="flex items-center justify-between mb-2">
                          <label className="text-xs font-semibold" style={{ color: 'var(--color-text-secondary)' }}>主模型</label>
                          <button
                            className="text-xs cursor-pointer"
                            style={{ color: 'var(--color-primary)' }}
                            onClick={() => addModelToGroup(providerName, gIdx)}
                          >
                            + 添加模型
                          </button>
                        </div>
                        <div className="space-y-2">
                          {group.models.map((model, mIdx) => (
                            <div key={mIdx} className="flex items-center gap-2">
                              <input
                                className="input flex-1"
                                placeholder="模型名称 (如: gpt-4o)"
                                value={model}
                                onChange={e => updateModelInGroup(providerName, gIdx, mIdx, e.target.value)}
                              />
                              <div className="flex items-center gap-1">
                                <input
                                  className="input"
                                  style={{ width: '120px', fontSize: '12px' }}
                                  type="text"
                                  inputMode="decimal"
                                  placeholder="Prompt $/tok"
                                  value={formatPrice(group.pricing[model]?.prompt)}
                                  onChange={e => updatePricing(providerName, gIdx, model, 'prompt', e.target.value)}
                                  title="Prompt 价格 ($/token)，如 0.000005"
                                />
                                <input
                                  className="input"
                                  style={{ width: '120px', fontSize: '12px' }}
                                  type="text"
                                  inputMode="decimal"
                                  placeholder="Compl $/tok"
                                  value={formatPrice(group.pricing[model]?.completion)}
                                  onChange={e => updatePricing(providerName, gIdx, model, 'completion', e.target.value)}
                                  title="Completion 价格 ($/token)，如 0.000015"
                                />
                              </div>
                              <button
                                className="p-1 rounded cursor-pointer"
                                style={{ color: 'var(--color-danger)' }}
                                onClick={() => removeModelFromGroup(providerName, gIdx, mIdx)}
                                title="移除模型"
                              >
                                <Trash2 size={14} />
                              </button>
                            </div>
                          ))}
                        </div>
                      </div>

                      {/* Fallback 模型 */}
                      <div>
                        <div className="flex items-center justify-between mb-2">
                          <label className="text-xs font-semibold" style={{ color: 'var(--color-text-secondary)' }}>降级模型 (Fallback)</label>
                          <button
                            className="text-xs cursor-pointer"
                            style={{ color: 'var(--color-primary)' }}
                            onClick={() => addFallbackToGroup(providerName, gIdx)}
                          >
                            + 添加降级模型
                          </button>
                        </div>
                        <div className="space-y-2">
                          {group.fallback_models.map((fb, fbIdx) => (
                            <div key={fbIdx} className="flex items-center gap-2">
                              <input
                                className="input flex-1"
                                placeholder="降级模型名称"
                                value={fb}
                                onChange={e => updateFallbackInGroup(providerName, gIdx, fbIdx, e.target.value)}
                              />
                              <button
                                className="p-1 rounded cursor-pointer"
                                style={{ color: 'var(--color-danger)' }}
                                onClick={() => removeFallbackFromGroup(providerName, gIdx, fbIdx)}
                              >
                                <Trash2 size={14} />
                              </button>
                            </div>
                          ))}
                          {group.fallback_models.length === 0 && (
                            <span className="text-xs" style={{ color: 'var(--color-text-quaternary)' }}>暂无降级模型</span>
                          )}
                        </div>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </Card>
        ))}
      </div>

      {/* --- Embedding 配置 --- */}
      <Card title="向量模型配置 (Embedding)">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <label className="block text-xs mb-1 font-medium" style={{ color: 'var(--color-text-tertiary)' }}>后端引擎</label>
            <select
              className="input w-full"
              value={embedding.backend}
              onChange={e => setEmbedding(prev => ({ ...prev, backend: e.target.value }))}
              style={{ cursor: 'pointer' }}
            >
              <option value="sentence_transformers">Sentence Transformers (本地)</option>
              <option value="openai">OpenAI API</option>
              <option value="litellm">LiteLLM</option>
            </select>
          </div>
          <div>
            <label className="block text-xs mb-1 font-medium" style={{ color: 'var(--color-text-tertiary)' }}>模型名称</label>
            <input
              className="input w-full"
              placeholder="Qwen/Qwen3-Embedding-0.6B"
              value={embedding.model}
              onChange={e => setEmbedding(prev => ({ ...prev, model: e.target.value }))}
            />
          </div>
          <div>
            <label className="block text-xs mb-1 font-medium" style={{ color: 'var(--color-text-tertiary)' }}>向量维度</label>
            <input
              className="input w-full"
              type="number"
              min={64}
              max={4096}
              value={embedding.vector_dim}
              onChange={e => setEmbedding(prev => ({ ...prev, vector_dim: parseInt(e.target.value) || 1024 }))}
            />
          </div>
          <div>
            <label className="block text-xs mb-1 font-medium" style={{ color: 'var(--color-text-tertiary)' }}>OpenAI Embedding 模型 (远程回退)</label>
            <input
              className="input w-full"
              placeholder="text-embedding-3-small"
              value={embedding.openai_model}
              onChange={e => setEmbedding(prev => ({ ...prev, openai_model: e.target.value }))}
            />
          </div>
        </div>
      </Card>
    </div>
  )
}
