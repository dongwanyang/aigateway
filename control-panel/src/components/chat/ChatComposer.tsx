import { useEffect, useState, useRef, type KeyboardEvent } from 'react'
import { RefreshCw, Send, Square } from 'lucide-react'
import type { GenerationPreset } from '@/api/client'
import type { GenerationOptions } from '@/types'

interface ChatComposerProps {
  streaming: boolean
  disabled: boolean
  onSend: (text: string, opts?: { generationOptions?: GenerationOptions }) => void
  onStop: () => void
  presets?: GenerationPreset[]
  presetsLoading?: boolean
  presetsError?: string | null
  onRefreshPresets?: () => void
}

function presetIsAvailable(preset: GenerationPreset): boolean {
  return preset.enabled
    && preset.selectable !== false
    && preset.validation.missing_models.length === 0
    && preset.validation.missing_nodes.length === 0
}

export default function ChatComposer({
  streaming,
  disabled,
  onSend,
  onStop,
  presets = [],
  presetsLoading = false,
  presetsError = null,
  onRefreshPresets,
}: ChatComposerProps) {
  const [text, setText] = useState('')
  const [backend, setBackend] = useState<GenerationOptions['backend']>('auto')
  const [presetId, setPresetId] = useState('')
  const [quality, setQuality] = useState<NonNullable<GenerationOptions['quality']>>('standard')
  const [size, setSize] = useState('')
  const taRef = useRef<HTMLTextAreaElement>(null)
  const imagePresets = Array.isArray(presets)
    ? presets.filter(preset => preset.kind === 'image')
    : []

  useEffect(() => {
    if (!presetId) return
    const selected = imagePresets.find(preset => preset.id === presetId)
    if (!selected || !presetIsAvailable(selected)) setPresetId('')
  }, [imagePresets, presetId])

  function submit() {
    const t = text.trim()
    if (!t || streaming || disabled) return
    if (backend === 'auto' && !presetId && quality === 'standard' && !size) {
      onSend(t)
    } else {
      const [width, height] = size
        ? size.split('x').map(value => Number(value))
        : [undefined, undefined]
      onSend(t, {
        generationOptions: {
          backend,
          preset_id: presetId || undefined,
          quality,
          prompt_mode: 'auto',
          width,
          height,
        },
      })
    }
    setText('')
    if (taRef.current) taRef.current.style.height = 'auto'
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.nativeEvent.isComposing) return
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      submit()
    }
  }

  function onInput() {
    const ta = taRef.current
    if (!ta) return
    ta.style.height = 'auto'
    ta.style.height = `${Math.min(ta.scrollHeight, 160)}px`
  }

  return (
    <div className="p-3" style={{ borderTop: '1px solid var(--color-border)' }}>
      <div className="flex flex-wrap gap-2 mb-2">
        <label className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
          后端{' '}
          <select
            value={backend}
            disabled={streaming || disabled}
            onChange={event => {
              const next = event.target.value as GenerationOptions['backend']
              setBackend(next)
              if (next === 'cloud') setPresetId('')
            }}
          >
            <option value="auto">自动</option>
            <option value="local">本地</option>
            <option value="cloud">云端</option>
          </select>
        </label>
        <label className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
          图片模型/预设{' '}
          <select
            aria-label="图片模型/预设"
            value={presetId}
            disabled={streaming || disabled || backend === 'cloud'}
            onChange={event => {
              setPresetId(event.target.value)
              if (event.target.value) setBackend('local')
            }}
          >
            <option value="">自动选择</option>
            {imagePresets.map(preset => {
              const available = presetIsAvailable(preset)
              const suffix = preset.source === 'discovered' ? ' · 已安装' : ''
              return (
                <option key={preset.id} value={preset.id} disabled={!available}>
                  {preset.name}{suffix}{available ? '' : ' · 不可用'}
                </option>
              )
            })}
          </select>
        </label>
        {onRefreshPresets && (
          <button
            type="button"
            aria-label="刷新图片模型"
            title="重新扫描本地图片模型"
            disabled={presetsLoading || streaming || disabled}
            onClick={onRefreshPresets}
            className="inline-flex items-center p-1 rounded cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
            style={{ color: 'var(--color-text-secondary)' }}
          >
            <RefreshCw size={14} className={presetsLoading ? 'animate-spin' : ''} />
          </button>
        )}
        <label className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
          图片质量{' '}
          <select disabled={streaming || disabled} value={quality} onChange={event => setQuality(event.target.value as NonNullable<GenerationOptions['quality']>)}>
            <option value="standard">标准</option>
            <option value="creative_refine">创意精修</option>
            <option value="faithful_4k">4K 保真</option>
          </select>
        </label>
        <label className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
          尺寸{' '}
          <select disabled={streaming || disabled} value={size} onChange={event => setSize(event.target.value)}>
            <option value="">自动</option>
            <option value="1024x1024">1024 × 1024</option>
            <option value="1344x768">1344 × 768</option>
            <option value="768x1344">768 × 1344</option>
          </select>
        </label>
      </div>
      {presetsError && (
        <div role="status" className="text-xs mb-2" style={{ color: 'var(--color-warning)' }}>
          图片模型列表加载失败，仍可使用自动选择：{presetsError}
        </div>
      )}
      <div className="flex items-end gap-2">
      <textarea
        ref={taRef}
        value={text}
        disabled={disabled}
        onChange={e => setText(e.target.value)}
        onInput={onInput}
        onKeyDown={onKeyDown}
        rows={1}
        placeholder={disabled ? '请先在任一页面设置 API Key' : '输入消息,Enter 发送 / Shift+Enter 换行'}
        className="flex-1 resize-none px-3 py-2 rounded-md outline-none"
        style={{
          backgroundColor: 'var(--color-bg-overlay)',
          color: 'var(--color-text-primary)',
          border: '1px solid var(--color-border)',
          maxHeight: '160px',
        }}
      />
      {streaming ? (
        <button
          onClick={onStop}
          className="flex items-center gap-1 px-3 py-2 rounded-md cursor-pointer"
          style={{ backgroundColor: 'var(--color-danger)', color: '#fff' }}
        >
          <Square size={16} /> 停止
        </button>
      ) : (
        <button
          onClick={submit}
          disabled={disabled || !text.trim()}
          className="flex items-center gap-1 px-3 py-2 rounded-md cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
          style={{ backgroundColor: 'var(--color-primary)', color: 'var(--color-text-inverse)' }}
        >
          <Send size={16} /> 发送
        </button>
      )}
      </div>
    </div>
  )
}
