import { useEffect, useState } from 'react'
import { Bot, Plus, Trash2, Save, RefreshCw, ChevronDown, ChevronRight, Wifi, List, Zap, Check, Pencil, X } from 'lucide-react'
import Card from '@/components/Card'
import { getFullConfig, updateFullConfig, testProviderConnectivity, fetchProviderModels } from '@/api/client'

// --- 预设提供商定义 ---

interface PresetProvider {
  id: string
  name: string
  description: string
  baseUrl: string
  defaultModels: string[]
  keyPlaceholder: string
  keyPrefix?: string
  color: string
}

const PRESET_PROVIDERS: PresetProvider[] = [
  {
    id: 'openai',
    name: 'OpenAI',
    description: 'GPT-4o, GPT-4o-mini, o1, o3 等',
    baseUrl: 'https://api.openai.com/v1',
    defaultModels: ['gpt-4o', 'gpt-4o-mini', 'o1', 'o3-mini', 'gpt-4-turbo'],
    keyPlaceholder: 'sk-...',
    keyPrefix: 'sk-',
    color: '#10a37f',
  },
  {
    id: 'anthropic',
    name: 'Anthropic (Claude)',
    description: 'Claude 4 Sonnet, Claude 3.5 Sonnet, Claude 3 Opus 等',
    baseUrl: 'https://api.anthropic.com/v1',
    defaultModels: ['claude-sonnet-4-20250514', 'claude-3-5-sonnet-20241022', 'claude-3-opus-20240229', 'claude-3-haiku-20240307'],
    keyPlaceholder: 'sk-ant-...',
    keyPrefix: 'sk-ant-',
    color: '#d97757',
  },
  {
    id: 'google',
    name: 'Google (Gemini)',
    description: 'Gemini 2.5 Pro, Gemini 2.0 Flash 等',
    baseUrl: 'https://generativelanguage.googleapis.com/v1beta/openai',
    defaultModels: ['gemini-2.5-pro', 'gemini-2.5-flash', 'gemini-2.0-flash', 'gemini-1.5-pro'],
    keyPlaceholder: 'AIza...',
    color: '#4285f4',
  },
  {
    id: 'deepseek',
    name: 'DeepSeek',
    description: 'DeepSeek-V3, DeepSeek-R1 等',
    baseUrl: 'https://api.deepseek.com/v1',
    defaultModels: ['deepseek-chat', 'deepseek-reasoner'],
    keyPlaceholder: 'sk-...',
    color: '#4d6bfe',
  },
  {
    id: 'zhipu',
    name: '智谱 AI (GLM)',
    description: 'GLM-4-Plus, GLM-4-Flash 等',
    baseUrl: 'https://open.bigmodel.cn/api/paas/v4',
    defaultModels: ['glm-4-plus', 'glm-4-flash', 'glm-4-long', 'glm-4v-plus'],
    keyPlaceholder: '输入你的智谱 API Key',
    color: '#3451b2',
  },
  {
    id: 'qwen',
    name: '通义千问 (Qwen)',
    description: 'Qwen-Max, Qwen-Plus, Qwen-Turbo 等',
    baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    defaultModels: ['qwen-max', 'qwen-plus', 'qwen-turbo', 'qwen-long'],
    keyPlaceholder: 'sk-...',
    color: '#6236ff',
  },
  {
    id: 'moonshot',
    name: 'Moonshot (Kimi)',
    description: 'Moonshot-v1-8k, 32k, 128k',
    baseUrl: 'https://api.moonshot.cn/v1',
    defaultModels: ['moonshot-v1-8k', 'moonshot-v1-32k', 'moonshot-v1-128k'],
    keyPlaceholder: 'sk-...',
    color: '#000000',
  },
  {
    id: 'doubao',
    name: '豆包 (Doubao)',
    description: '字节跳动豆包大模型',
    baseUrl: 'https://ark.cn-beijing.volces.com/api/v3',
    defaultModels: ['doubao-1-5-pro-256k', 'doubao-1-5-pro-32k', 'doubao-1-5-lite-32k'],
    keyPlaceholder: '输入你的火山引擎 API Key',
    color: '#ff6900',
  },
  {
    id: 'yi',
    name: '零一万物 (Yi)',
    description: 'Yi-Lightning, Yi-Large, Yi-Medium 等',
    baseUrl: 'https://api.lingyiwanwu.com/v1',
    defaultModels: ['yi-lightning', 'yi-large', 'yi-medium', 'yi-spark'],
    keyPlaceholder: '输入你的零一万物 API Key',
    color: '#1a1a2e',
  },
  {
    id: 'minimax',
    name: 'MiniMax',
    description: 'abab6.5s, abab6.5t 等',
    baseUrl: 'https://api.minimax.chat/v1',
    defaultModels: ['abab6.5s-chat', 'abab6.5t-chat', 'abab5.5-chat'],
    keyPlaceholder: '输入你的 MiniMax API Key',
    color: '#e83e8c',
  },
  {
    id: 'groq',
    name: 'Groq',
    description: 'Llama 3.3, Mixtral 等（极速推理）',
    baseUrl: 'https://api.groq.com/openai/v1',
    defaultModels: ['llama-3.3-70b-versatile', 'llama-3.1-8b-instant', 'mixtral-8x7b-32768'],
    keyPlaceholder: 'gsk_...',
    keyPrefix: 'gsk_',
    color: '#f55036',
  },
  {
    id: 'mistral',
    name: 'Mistral AI',
    description: 'Mistral Large, Medium, Small 等',
    baseUrl: 'https://api.mistral.ai/v1',
    defaultModels: ['mistral-large-latest', 'mistral-medium-latest', 'mistral-small-latest', 'open-mixtral-8x22b'],
    keyPlaceholder: '输入你的 Mistral API Key',
    color: '#ff7000',
  },
  {
    id: 'openrouter',
    name: 'OpenRouter',
    description: '聚合多个模型提供商的统一接口',
    baseUrl: 'https://openrouter.ai/api/v1',
    defaultModels: ['openai/gpt-4o', 'anthropic/claude-3.5-sonnet', 'google/gemini-pro-1.5', 'meta-llama/llama-3.1-405b-instruct'],
    keyPlaceholder: 'sk-or-...',
    keyPrefix: 'sk-or-',
    color: '#6366f1',
  },
  {
    id: 'siliconflow',
    name: 'SiliconFlow (硅基流动)',
    description: 'DeepSeek, Qwen, GLM 等开源模型托管',
    baseUrl: 'https://api.siliconflow.cn/v1',
    defaultModels: ['deepseek-ai/DeepSeek-V3', 'Qwen/Qwen2.5-72B-Instruct', 'THUDM/glm-4-9b-chat'],
    keyPlaceholder: 'sk-...',
    color: '#7c3aed',
  },
  {
    id: 'custom',
    name: '自定义 (OpenAI 兼容)',
    description: '其他兼容 OpenAI API 格式的服务',
    baseUrl: '',
    defaultModels: [],
    keyPlaceholder: '输入 API Key',
    color: '#6b7280',
  },
]

// --- 类型定义 ---

// 支持的 modality 分类
const MODALITY_OPTIONS = ['llm', 'mllm', 'generative'] as const
type Modality = typeof MODALITY_OPTIONS[number]

const MODALITY_LABEL: Record<Modality, string> = {
  llm: '纯文本 (llm)',
  mllm: '多模态理解 (mllm)',
  generative: '生成 (generative)',
}

interface ModelEntry {
  name: string
  modality: string[]
  base_url?: string            // 可选：per-model base_url 覆盖，留空继承提供商级别
}

interface PricingConfig {
  prompt: number
  completion: number
}

interface ModelGroup {
  models: ModelEntry[]
  fallback_models: string[]
  pricing: Record<string, PricingConfig>
}

interface ProviderConfig {
  api_key: string
  base_url?: string
  model_grouper: ModelGroup[]
  num_retries: number
  retry_after: number
  timeout: number
}

interface EmbeddingConfig {
  backend: string
  model: string
  vector_dim: number
  openai_model: string
}

// 归一化任意 config 中的 model_entry -> ModelEntry
function normalizeModelEntry(raw: any): ModelEntry | null {
  if (!raw) return null
  if (typeof raw === 'string') {
    return { name: raw, modality: [] }
  }
  if (typeof raw === 'object') {
    const name = String(raw.name ?? '').trim()
    if (!name) return null
    let modality: string[] = []
    if (Array.isArray(raw.modality)) {
      modality = raw.modality.map((x: any) => String(x)).filter(Boolean)
    } else if (typeof raw.modality === 'string' && raw.modality) {
      // 旧字符串写法：加载时容错为单元素列表，保存时以列表形式回写
      modality = [raw.modality]
    }
    const baseUrlRaw = typeof raw.base_url === 'string' ? raw.base_url.trim() : ''
    return { name, modality, base_url: baseUrlRaw || undefined }
  }
  return null
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
  const [showQuickAdd, setShowQuickAdd] = useState(false)
  const [quickAddStep, setQuickAddStep] = useState<'select' | 'config'>('select')
  const [selectedPreset, setSelectedPreset] = useState<PresetProvider | null>(null)
  const [quickAddKey, setQuickAddKey] = useState('')
  const [quickAddBaseUrl, setQuickAddBaseUrl] = useState('')
  const [quickAddName, setQuickAddName] = useState('')
  const [fullConfig, setFullConfig] = useState<Record<string, unknown>>({})
  const [testResults, setTestResults] = useState<Record<string, { success: boolean; latency_ms: number; error?: string; loading: boolean }>>({})
  const [fetchedModels, setFetchedModels] = useState<Record<string, { models: string[]; loading: boolean; error?: string }>>({})

  // --- 新增/编辑模型弹窗 ---
  // mode='add' 时 modelIdx=-1；mode='edit' 时 modelIdx 指向原索引
  const [modelDialog, setModelDialog] = useState<{
    providerName: string
    groupIdx: number
    modelIdx: number
    mode: 'add' | 'edit'
    name: string
    modality: string[]
    promptPrice: string
    completionPrice: string
    baseUrl: string
  } | null>(null)

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
          const rawGroups = Array.isArray(cfg.model_grouper) ? cfg.model_grouper : []
          const groups: ModelGroup[] = rawGroups.map((g: any) => {
            const rawModels = Array.isArray(g?.models) ? g.models : []
            const models: ModelEntry[] = rawModels
              .map(normalizeModelEntry)
              .filter((m: ModelEntry | null): m is ModelEntry => m !== null)
            const fallbackModels = Array.isArray(g?.fallback_models)
              ? g.fallback_models.map((f: any) => String(f))
              : []
            const pricing = typeof g?.pricing === 'object' && g?.pricing !== null
              ? { ...g.pricing }
              : {}
            return { models, fallback_models: fallbackModels, pricing }
          })
          parsed[name] = {
            api_key: cfg.api_key ?? '',
            base_url: cfg.base_url ?? '',
            model_grouper: groups,
            num_retries: cfg.num_retries ?? 3,
            retry_after: cfg.retry_after ?? 1000,
            timeout: cfg.timeout ?? 120,
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

  // --- 快速添加提供商 ---
  function handleSelectPreset(preset: PresetProvider) {
    setSelectedPreset(preset)
    setQuickAddKey('')
    setQuickAddBaseUrl(preset.baseUrl)
    setQuickAddName(preset.id === 'custom' ? '' : preset.id)
    setQuickAddStep('config')
  }

  function handleQuickAddConfirm() {
    if (!selectedPreset) return
    const name = (quickAddName || selectedPreset.id).trim().toLowerCase()
    if (!name || providers[name]) {
      setError(`提供商 "${name}" 已存在`)
      return
    }
    if (!quickAddKey.trim()) {
      setError('请输入 API Key')
      return
    }

    const newProvider: ProviderConfig = {
      api_key: quickAddKey.trim(),
      base_url: quickAddBaseUrl || selectedPreset.baseUrl || '',
      model_grouper: [{
        models: selectedPreset.defaultModels.map(name => ({
          name,
          modality: ['llm'],   // 默认给 llm，用户可在编辑弹窗中调整
        })),
        fallback_models: [],
        pricing: {},
      }],
      num_retries: 3,
      retry_after: 1000,
      timeout: 120,
    }

    setProviders(prev => ({ ...prev, [name]: newProvider }))
    setExpandedProviders(prev => new Set([...prev, name]))
    setShowQuickAdd(false)
    setQuickAddStep('select')
    setSelectedPreset(null)
    setQuickAddKey('')
    setSuccess(`已添加提供商 "${name}"，记得点击"保存配置"使其生效`)
    setTimeout(() => setSuccess(null), 4000)
  }

  function resetQuickAdd() {
    setShowQuickAdd(false)
    setQuickAddStep('select')
    setSelectedPreset(null)
    setQuickAddKey('')
    setQuickAddBaseUrl('')
    setQuickAddName('')
  }

  // --- 通用操作 ---
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

  function updateModelInGroup(
    providerName: string,
    groupIdx: number,
    modelIdx: number,
    patch: Partial<ModelEntry>,
  ) {
    setProviders(prev => {
      const p = { ...prev[providerName] }
      const groups = [...p.model_grouper]
      const group = { ...groups[groupIdx] }
      const models = [...group.models]
      const prevEntry = models[modelIdx]
      if (!prevEntry) return prev
      const nextEntry: ModelEntry = {
        name: patch.name !== undefined ? patch.name : prevEntry.name,
        modality: patch.modality !== undefined ? patch.modality : prevEntry.modality,
        base_url: patch.base_url !== undefined ? patch.base_url : prevEntry.base_url,
      }
      // 如果 name 发生变化，同步迁移 pricing key
      if (patch.name !== undefined && patch.name !== prevEntry.name) {
        const pricing = { ...group.pricing }
        if (prevEntry.name in pricing) {
          pricing[nextEntry.name] = pricing[prevEntry.name]
          delete pricing[prevEntry.name]
        }
        group.pricing = pricing
      }
      models[modelIdx] = nextEntry
      group.models = models
      groups[groupIdx] = group
      p.model_grouper = groups
      return { ...prev, [providerName]: p }
    })
  }

  function addModelToGroup(
    providerName: string,
    groupIdx: number,
    entry: ModelEntry = { name: '', modality: [] },
  ) {
    setProviders(prev => {
      const p = { ...prev[providerName] }
      const groups = [...p.model_grouper]
      const group = { ...groups[groupIdx] }
      // 过滤掉值为 undefined 的可选字段，保持 config 干净
      const cleanEntry: ModelEntry = { name: entry.name, modality: entry.modality }
      if (entry.base_url) cleanEntry.base_url = entry.base_url
      group.models = [...group.models, cleanEntry]
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
      const modelName = group.models[modelIdx]?.name
      group.models = group.models.filter((_, i) => i !== modelIdx)
      if (modelName) {
        const pricing = { ...group.pricing }
        delete pricing[modelName]
        group.pricing = pricing
      }
      groups[groupIdx] = group
      p.model_grouper = groups
      return { ...prev, [providerName]: p }
    })
  }

  // --- 新增/编辑模型弹窗控制 ---
  function openAddModelDialog(providerName: string, groupIdx: number, initialName = '') {
    setModelDialog({
      providerName,
      groupIdx,
      modelIdx: -1,
      mode: 'add',
      name: initialName,
      modality: initialName ? ['llm'] : [],
      promptPrice: '',
      completionPrice: '',
      baseUrl: '',
    })
  }

  function openEditModelDialog(providerName: string, groupIdx: number, modelIdx: number) {
    const group = providers[providerName]?.model_grouper[groupIdx]
    const entry = group?.models[modelIdx]
    if (!entry) return
    const pricing = group?.pricing?.[entry.name]
    setModelDialog({
      providerName,
      groupIdx,
      modelIdx,
      mode: 'edit',
      name: entry.name,
      modality: [...entry.modality],
      promptPrice: pricing ? formatPrice(pricing.prompt) : '',
      completionPrice: pricing ? formatPrice(pricing.completion) : '',
      baseUrl: entry.base_url ?? '',
    })
  }

  function closeModelDialog() {
    setModelDialog(null)
  }

  function toggleDialogModality(m: string) {
    setModelDialog(prev => {
      if (!prev) return prev
      const has = prev.modality.includes(m)
      return {
        ...prev,
        modality: has ? prev.modality.filter(x => x !== m) : [...prev.modality, m],
      }
    })
  }

  function commitModelDialog() {
    if (!modelDialog) return
    const trimmedName = modelDialog.name.trim()
    if (!trimmedName) {
      setError('请填写模型名称')
      return
    }
    if (modelDialog.modality.length === 0) {
      setError('请至少选择一个 modality')
      return
    }

    const { providerName, groupIdx, modelIdx, mode } = modelDialog
    const group = providers[providerName]?.model_grouper[groupIdx]
    if (!group) return

    // 检查重名（编辑时允许保持原名）
    const duplicate = group.models.some(
      (m, i) => m.name === trimmedName && !(mode === 'edit' && i === modelIdx),
    )
    if (duplicate) {
      setError(`模型 "${trimmedName}" 已存在`)
      return
    }

    const promptNum = modelDialog.promptPrice === '' ? 0 : parseFloat(modelDialog.promptPrice)
    const completionNum = modelDialog.completionPrice === '' ? 0 : parseFloat(modelDialog.completionPrice)
    const priceValid =
      (modelDialog.promptPrice === '' || !isNaN(promptNum)) &&
      (modelDialog.completionPrice === '' || !isNaN(completionNum))
    if (!priceValid) {
      setError('价格格式不合法')
      return
    }

    if (mode === 'add') {
      addModelToGroup(providerName, groupIdx, {
        name: trimmedName,
        modality: [...modelDialog.modality],
        base_url: modelDialog.baseUrl.trim() || undefined,
      })
    } else {
      updateModelInGroup(providerName, groupIdx, modelIdx, {
        name: trimmedName,
        modality: [...modelDialog.modality],
        base_url: modelDialog.baseUrl.trim() || undefined,
      })
    }

    // 同步 pricing（走已有的 updatePricing）
    updatePricing(providerName, groupIdx, trimmedName, 'prompt', String(promptNum || 0))
    updatePricing(providerName, groupIdx, trimmedName, 'completion', String(completionNum || 0))

    setError(null)
    setModelDialog(null)
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

  function formatPrice(value: number | undefined): string {
    if (value === undefined || value === 0) return ''
    const str = value.toFixed(10).replace(/0+$/, '').replace(/\.$/, '')
    return str
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

      {/* === 快速添加提供商面板 === */}
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <h3 className="text-lg font-semibold">模型提供商</h3>
          <button
            className="btn btn-primary"
            style={{ padding: '8px 16px', fontSize: '12px' }}
            onClick={() => { setShowQuickAdd(true); setQuickAddStep('select') }}
          >
            <Zap size={14} /> 快速添加
          </button>
        </div>

        {/* 快速添加 - 选择提供商 */}
        {showQuickAdd && quickAddStep === 'select' && (
          <Card>
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <h4 className="font-semibold text-base">选择模型提供商</h4>
                <button
                  className="text-xs cursor-pointer px-3 py-1 rounded"
                  style={{ color: 'var(--color-text-secondary)', border: '1px solid var(--color-border)' }}
                  onClick={resetQuickAdd}
                >
                  取消
                </button>
              </div>
              <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                选择一个提供商，只需输入 API Key 即可完成配置，无需手动填写 Base URL
              </p>
              <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
                {PRESET_PROVIDERS.map(preset => {
                  const alreadyAdded = Object.keys(providers).includes(preset.id)
                  return (
                    <div
                      key={preset.id}
                      className="relative p-4 rounded-lg cursor-pointer transition-all"
                      style={{
                        border: `1.5px solid ${alreadyAdded ? 'var(--color-border)' : 'var(--color-border)'}`,
                        backgroundColor: 'var(--color-bg-overlay)',
                        opacity: alreadyAdded ? 0.5 : 1,
                      }}
                      onClick={() => !alreadyAdded && handleSelectPreset(preset)}
                      onMouseEnter={e => {
                        if (!alreadyAdded) {
                          (e.currentTarget as HTMLElement).style.borderColor = preset.color
                          ;(e.currentTarget as HTMLElement).style.boxShadow = `0 0 0 1px ${preset.color}20`
                        }
                      }}
                      onMouseLeave={e => {
                        (e.currentTarget as HTMLElement).style.borderColor = 'var(--color-border)'
                        ;(e.currentTarget as HTMLElement).style.boxShadow = 'none'
                      }}
                    >
                      {alreadyAdded && (
                        <div className="absolute top-2 right-2">
                          <Check size={14} style={{ color: 'var(--color-success)' }} />
                        </div>
                      )}
                      <div
                        className="w-8 h-8 rounded-lg flex items-center justify-center mb-2 text-white font-bold text-sm"
                        style={{ backgroundColor: preset.color }}
                      >
                        {preset.name.charAt(0)}
                      </div>
                      <div className="font-medium text-sm">{preset.name}</div>
                      <div className="text-xs mt-1" style={{ color: 'var(--color-text-quaternary)' }}>
                        {preset.description}
                      </div>
                      {alreadyAdded && (
                        <div className="text-xs mt-1" style={{ color: 'var(--color-success)' }}>已配置</div>
                      )}
                    </div>
                  )
                })}
              </div>
            </div>
          </Card>
        )}

        {/* 快速添加 - 输入 API Key */}
        {showQuickAdd && quickAddStep === 'config' && selectedPreset && (
          <Card>
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <div
                    className="w-8 h-8 rounded-lg flex items-center justify-center text-white font-bold text-sm"
                    style={{ backgroundColor: selectedPreset.color }}
                  >
                    {selectedPreset.name.charAt(0)}
                  </div>
                  <div>
                    <h4 className="font-semibold text-base">{selectedPreset.name}</h4>
                    <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                      {selectedPreset.description}
                    </p>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    className="text-xs cursor-pointer px-3 py-1 rounded"
                    style={{ color: 'var(--color-text-secondary)', border: '1px solid var(--color-border)' }}
                    onClick={() => setQuickAddStep('select')}
                  >
                    返回
                  </button>
                  <button
                    className="text-xs cursor-pointer px-3 py-1 rounded"
                    style={{ color: 'var(--color-text-secondary)', border: '1px solid var(--color-border)' }}
                    onClick={resetQuickAdd}
                  >
                    取消
                  </button>
                </div>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                  <label className="block text-xs mb-1 font-medium" style={{ color: 'var(--color-text-tertiary)' }}>
                    API Key <span style={{ color: 'var(--color-danger)' }}>*</span>
                  </label>
                  <input
                    className="input w-full"
                    type="password"
                    placeholder={selectedPreset.keyPlaceholder}
                    value={quickAddKey}
                    onChange={e => setQuickAddKey(e.target.value)}
                    onKeyDown={e => { if (e.key === 'Enter') handleQuickAddConfirm() }}
                    autoFocus
                  />
                </div>
                {selectedPreset.id === 'custom' && (
                  <div>
                    <label className="block text-xs mb-1 font-medium" style={{ color: 'var(--color-text-tertiary)' }}>
                      提供商名称 <span style={{ color: 'var(--color-danger)' }}>*</span>
                    </label>
                    <input
                      className="input w-full"
                      placeholder="如: my-provider"
                      value={quickAddName}
                      onChange={e => setQuickAddName(e.target.value)}
                    />
                  </div>
                )}
                {selectedPreset.id === 'custom' && (
                  <div>
                    <label className="block text-xs mb-1 font-medium" style={{ color: 'var(--color-text-tertiary)' }}>
                      Base URL <span style={{ color: 'var(--color-danger)' }}>*</span>
                    </label>
                    <input
                      className="input w-full"
                      placeholder="https://your-api.com/v1"
                      value={quickAddBaseUrl}
                      onChange={e => setQuickAddBaseUrl(e.target.value)}
                    />
                  </div>
                )}
              </div>

              {/* 预设模型列表预览 */}
              {selectedPreset.defaultModels.length > 0 && (
                <div>
                  <label className="block text-xs mb-2 font-medium" style={{ color: 'var(--color-text-tertiary)' }}>
                    将自动添加以下模型:
                  </label>
                  <div className="flex flex-wrap gap-2">
                    {selectedPreset.defaultModels.map(m => (
                      <span
                        key={m}
                        className="text-xs px-2.5 py-1 rounded-md"
                        style={{
                          backgroundColor: `${selectedPreset.color}15`,
                          border: `1px solid ${selectedPreset.color}30`,
                          color: 'var(--color-text-primary)',
                        }}
                      >
                        {m}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {/* Base URL 显示（非自定义时只读展示） */}
              {selectedPreset.id !== 'custom' && selectedPreset.baseUrl && (
                <div className="text-xs p-2 rounded" style={{ backgroundColor: 'var(--color-bg-elevated)', color: 'var(--color-text-quaternary)' }}>
                  🔗 Base URL: <code>{selectedPreset.baseUrl}</code> (已自动配置，无需修改)
                </div>
              )}

              <div className="flex justify-end">
                <button
                  className="btn btn-primary"
                  style={{ padding: '10px 24px', fontSize: '13px' }}
                  onClick={handleQuickAddConfirm}
                  disabled={!quickAddKey.trim() || (selectedPreset.id === 'custom' && !quickAddBaseUrl.trim())}
                >
                  <Plus size={14} /> 确认添加
                </button>
              </div>
            </div>
          </Card>
        )}

        {/* === 已配置的提供商列表 === */}
        {Object.entries(providers).map(([providerName, config]) => (
          <Card key={providerName}>
            {/* Provider 头部 */}
            <div
              className="flex items-center justify-between cursor-pointer"
              onClick={() => toggleProviderExpand(providerName)}
            >
              <div className="flex items-center gap-3">
                {expandedProviders.has(providerName) ? <ChevronDown size={18} /> : <ChevronRight size={18} />}
                <Bot size={20} style={{ color: PRESET_PROVIDERS.find(p => p.id === providerName)?.color ?? 'var(--color-primary)' }} />
                <span className="font-semibold text-base">{providerName}</span>
                <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                  {config.model_grouper.flatMap(g => g.models).length} 个模型
                </span>
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
                              title="点击后打开新增弹窗"
                              onClick={() => {
                                const group = config.model_grouper[0]
                                if (!group) return
                                if (group.models.some(existing => existing.name === m)) return
                                openAddModelDialog(providerName, 0, m)
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

                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  <div>
                    <label className="block text-xs mb-1 font-medium" style={{ color: 'var(--color-text-tertiary)' }}>请求超时 (秒)</label>
                    <input
                      className="input w-full"
                      type="number"
                      min={5}
                      max={600}
                      value={config.timeout}
                      onChange={e => updateProvider(providerName, 'timeout', parseInt(e.target.value) || 120)}
                    />
                    <span className="text-xs" style={{ color: 'var(--color-text-quaternary)' }}>单次 LLM 请求最大等待时间</span>
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
                            onClick={() => openAddModelDialog(providerName, gIdx)}
                          >
                            + 添加模型
                          </button>
                        </div>
                        <div className="space-y-2">
                          {group.models.map((model, mIdx) => {
                            const pricing = group.pricing[model.name]
                            return (
                              <div
                                key={mIdx}
                                className="flex items-center gap-2 p-2 rounded"
                                style={{ border: '1px solid var(--color-border)', backgroundColor: 'var(--color-bg-elevated)' }}
                              >
                                <div className="flex-1 min-w-0">
                                  <div className="text-sm font-medium truncate">
                                    {model.name || <span style={{ color: 'var(--color-text-quaternary)' }}>（未命名模型）</span>}
                                  </div>
                                  <div className="flex flex-wrap items-center gap-1 mt-1">
                                    {model.modality.length === 0 ? (
                                      <span className="text-xs" style={{ color: 'var(--color-text-quaternary)' }}>无 modality</span>
                                    ) : model.modality.map(m => (
                                      <span
                                        key={m}
                                        className="text-xs px-1.5 py-0.5 rounded"
                                        style={{
                                          backgroundColor: 'rgba(59, 130, 246, 0.1)',
                                          color: 'var(--color-primary)',
                                          border: '1px solid rgba(59, 130, 246, 0.3)',
                                        }}
                                      >
                                        {m}
                                      </span>
                                    ))}
                                    {model.base_url ? (
                                      <span
                                        className="text-xs px-1.5 py-0.5 rounded"
                                        title={model.base_url}
                                        style={{
                                          backgroundColor: 'rgba(245, 158, 11, 0.1)',
                                          color: '#d97706',
                                          border: '1px solid rgba(245, 158, 11, 0.3)',
                                        }}
                                      >
                                        自定义URL
                                      </span>
                                    ) : null}
                                    {pricing && (pricing.prompt || pricing.completion) ? (
                                      <span className="text-xs ml-2" style={{ color: 'var(--color-text-quaternary)' }}>
                                        ${formatPrice(pricing.prompt) || 0} / ${formatPrice(pricing.completion) || 0} 每 token
                                      </span>
                                    ) : null}
                                  </div>
                                </div>
                                <button
                                  className="p-1 rounded cursor-pointer"
                                  style={{ color: 'var(--color-primary)' }}
                                  onClick={() => openEditModelDialog(providerName, gIdx, mIdx)}
                                  title="编辑模型"
                                >
                                  <Pencil size={14} />
                                </button>
                                <button
                                  className="p-1 rounded cursor-pointer"
                                  style={{ color: 'var(--color-danger)' }}
                                  onClick={() => removeModelFromGroup(providerName, gIdx, mIdx)}
                                  title="移除模型"
                                >
                                  <Trash2 size={14} />
                                </button>
                              </div>
                            )
                          })}
                          {group.models.length === 0 && (
                            <span className="text-xs" style={{ color: 'var(--color-text-quaternary)' }}>暂无模型，点击右上角"+ 添加模型"</span>
                          )}
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

      {/* === 新增/编辑模型弹窗 === */}
      {modelDialog && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center"
          style={{ backgroundColor: 'rgba(0, 0, 0, 0.5)' }}
          onClick={closeModelDialog}
        >
          <div
            className="rounded-lg p-5 w-full max-w-md space-y-4"
            style={{ backgroundColor: 'var(--color-bg-elevated)', border: '1px solid var(--color-border)' }}
            onClick={e => e.stopPropagation()}
          >
            <div className="flex items-center justify-between">
              <h3 className="text-base font-semibold">
                {modelDialog.mode === 'add' ? '新增模型' : '编辑模型'}
                <span className="text-xs ml-2" style={{ color: 'var(--color-text-quaternary)' }}>
                  {modelDialog.providerName}
                </span>
              </h3>
              <button
                className="p-1 rounded cursor-pointer"
                style={{ color: 'var(--color-text-secondary)' }}
                onClick={closeModelDialog}
                title="关闭"
              >
                <X size={16} />
              </button>
            </div>

            <div>
              <label className="block text-xs mb-1 font-medium" style={{ color: 'var(--color-text-tertiary)' }}>
                模型名称 <span style={{ color: 'var(--color-danger)' }}>*</span>
              </label>
              <input
                className="input w-full"
                placeholder="如: gpt-4o"
                value={modelDialog.name}
                autoFocus
                onChange={e => setModelDialog(prev => prev ? { ...prev, name: e.target.value } : prev)}
              />
            </div>

            <div>
              <label className="block text-xs mb-1 font-medium" style={{ color: 'var(--color-text-tertiary)' }}>
                Modality <span style={{ color: 'var(--color-danger)' }}>*</span>
                <span className="ml-2" style={{ color: 'var(--color-text-quaternary)' }}>
                  可多选，代表该模型支持的能力
                </span>
              </label>
              <div className="flex flex-col gap-1 rounded p-2" style={{ border: '1px solid var(--color-border)', backgroundColor: 'var(--color-bg-overlay)' }}>
                {MODALITY_OPTIONS.map(m => {
                  const checked = modelDialog.modality.includes(m)
                  return (
                    <label
                      key={m}
                      className="flex items-center gap-2 cursor-pointer text-sm px-2 py-1 rounded"
                      style={{
                        backgroundColor: checked ? 'rgba(59, 130, 246, 0.08)' : 'transparent',
                      }}
                    >
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => toggleDialogModality(m)}
                      />
                      <span>{MODALITY_LABEL[m]}</span>
                    </label>
                  )
                })}
              </div>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-xs mb-1 font-medium" style={{ color: 'var(--color-text-tertiary)' }}>
                  Prompt 单价 ($/token)
                </label>
                <input
                  className="input w-full"
                  type="text"
                  inputMode="decimal"
                  placeholder="0.000005"
                  value={modelDialog.promptPrice}
                  onChange={e => {
                    const v = e.target.value
                    if (v !== '' && !/^[0-9]*\.?[0-9]*$/.test(v)) return
                    setModelDialog(prev => prev ? { ...prev, promptPrice: v } : prev)
                  }}
                />
              </div>
              <div>
                <label className="block text-xs mb-1 font-medium" style={{ color: 'var(--color-text-tertiary)' }}>
                  Completion 单价 ($/token)
                </label>
                <input
                  className="input w-full"
                  type="text"
                  inputMode="decimal"
                  placeholder="0.000015"
                  value={modelDialog.completionPrice}
                  onChange={e => {
                    const v = e.target.value
                    if (v !== '' && !/^[0-9]*\.?[0-9]*$/.test(v)) return
                    setModelDialog(prev => prev ? { ...prev, completionPrice: v } : prev)
                  }}
                />
              </div>
            </div>

            <div>
              <label className="block text-xs mb-1 font-medium" style={{ color: 'var(--color-text-tertiary)' }}>
                Base URL
                <span className="ml-2" style={{ color: 'var(--color-text-quaternary)' }}>
                  可选覆盖，留空则使用提供商级别 URL
                </span>
              </label>
              <input
                className="input w-full"
                type="text"
                placeholder="https://api.example.com/v1 （留空=继承提供商）"
                value={modelDialog.baseUrl}
                onChange={e => setModelDialog(prev => prev ? { ...prev, baseUrl: e.target.value } : prev)}
              />
            </div>

            <div className="flex justify-end gap-2 pt-2">
              <button
                className="text-xs cursor-pointer px-3 py-1.5 rounded"
                style={{ color: 'var(--color-text-secondary)', border: '1px solid var(--color-border)' }}
                onClick={closeModelDialog}
              >
                取消
              </button>
              <button
                className="btn btn-primary"
                style={{ padding: '6px 16px', fontSize: '12px' }}
                onClick={commitModelDialog}
              >
                {modelDialog.mode === 'add' ? '添加' : '保存'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
