import { useState, useRef, type KeyboardEvent } from 'react'
import { Send, Square } from 'lucide-react'

interface ChatComposerProps {
  streaming: boolean
  disabled: boolean
  onSend: (text: string) => void
  onStop: () => void
}

export default function ChatComposer({ streaming, disabled, onSend, onStop }: ChatComposerProps) {
  const [text, setText] = useState('')
  const taRef = useRef<HTMLTextAreaElement>(null)

  function submit() {
    const t = text.trim()
    if (!t || streaming || disabled) return
    onSend(t)
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
    <div className="flex items-end gap-2 p-3" style={{ borderTop: '1px solid var(--color-border)' }}>
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
  )
}
