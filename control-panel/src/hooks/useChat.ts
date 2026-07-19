import { useCallback, useEffect, useRef, useState } from 'react'
import { createChatCompletionStream } from '@/api/client'
import type { ChatPageMessage, ChatMessage } from '@/types'

const STORAGE_KEY = 'aigateway:chat:messages'

function loadMessages(): ChatPageMessage[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed as ChatPageMessage[]
  } catch {
    return []
  }
}

let idCounter = 0
function nextId(): string {
  idCounter += 1
  return `msg-${Date.now()}-${idCounter}`
}

export interface UseChat {
  messages: ChatPageMessage[]
  streaming: boolean
  error: string | null
  send: (text: string) => Promise<void>
  stop: () => void
  clear: () => void
}

export function useChat(): UseChat {
  const [messages, setMessages] = useState<ChatPageMessage[]>(loadMessages)
  const [streaming, setStreaming] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  // 防重复发送:send 同步设置 true,确保双击/快速连续点击不会产生
  // 两个并发 fetch(否则两个助手占位 + 两次 API 调用)。
  const inflightRef = useRef(false)

  // 组件卸载时若仍在流式,abort 上游 fetch(否则离开 /chat 后请求继续跑到结束,
  // 白扣 token / 配额)。signal 已透传进 createChatCompletionStream。
  useEffect(() => {
    return () => {
      abortRef.current?.abort()
      abortRef.current = null
    }
  }, [])

  // debounce 持久化:messages 变更后 500ms 写一次
  useEffect(() => {
    const t = setTimeout(() => {
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(messages))
      } catch {
        // quota / 序列化失败,静默忽略
      }
    }, 500)
    return () => clearTimeout(t)
  }, [messages])

  const stop = useCallback(() => {
    abortRef.current?.abort()
    abortRef.current = null
    setStreaming(false)
  }, [])

  const clear = useCallback(() => {
    stop()
    setMessages([])
    try {
      localStorage.removeItem(STORAGE_KEY)
    } catch {
      // 忽略
    }
  }, [stop])

  const send = useCallback(async (text: string) => {
    const trimmed = text.trim()
    if (!trimmed || streaming || inflightRef.current) return
    inflightRef.current = true

    setError(null)

    const userMsg: ChatPageMessage = {
      id: nextId(), role: 'user', content: trimmed, ts: Date.now(),
    }
    const assistantId = nextId()
    const assistantMsg: ChatPageMessage = {
      id: assistantId, role: 'assistant', content: '', ts: Date.now(),
    }

    // 历史 wire 格式(发给后端)
    const wireMessages: ChatMessage[] = [...messages, userMsg].map(m => ({
      role: m.role, content: m.content,
    }))

    setMessages(prev => [...prev, userMsg, assistantMsg])
    setStreaming(true)

    const controller = new AbortController()
    abortRef.current = controller

    let reader: ReadableStreamDefaultReader<Uint8Array> | null = null

    try {
      const stream = await createChatCompletionStream(
        { model: 'auto', messages: wireMessages, stream: true },
        controller.signal,
      )
      reader = stream.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        // 按 SSE 帧分隔 \n\n
        let idx: number
        while ((idx = buffer.indexOf('\n\n')) !== -1) {
          const frame = buffer.slice(0, idx)
          buffer = buffer.slice(idx + 2)
          const line = frame.trim()
          if (!line.startsWith('data:')) continue
          const payload = line.slice(5).trim()
          if (payload === '[DONE]') {
            setStreaming(false)
            abortRef.current = null
            inflightRef.current = false
            reader.releaseLock()
            return
          }
          try {
            const chunk = JSON.parse(payload)
            const delta = chunk?.choices?.[0]?.delta
            const meta = chunk?._meta?.routed_to
            const isErr = !!chunk?.error
            setMessages(prev => prev.map(m => {
              if (m.id !== assistantId) return m
              const next: ChatPageMessage = { ...m }
              if (delta?.content) next.content += delta.content
              if (meta?.intent && !next.intent) next.intent = meta.intent
              if (meta?.model && !next.model) next.model = meta.model
              if (isErr) next.error = true
              return next
            }))
          } catch {
            // 非 JSON 帧,跳过
          }
        }
      }
      // 流自然结束(未收到 [DONE])
      setStreaming(false)
    } catch (e) {
      if (controller.signal.aborted) {
        // 用户主动停止,保留已收内容
        reader?.releaseLock()
        setStreaming(false)
      } else {
        const msg = e instanceof Error ? e.message : '请求失败'
        setError(msg)
        // 移除空的占位助手消息
        setMessages(prev => prev.filter(m => !(m.id === assistantId && m.content === '')))
        setStreaming(false)
      }
    } finally {
      reader?.releaseLock()
      abortRef.current = null
      inflightRef.current = false
    }
  }, [messages, streaming])

  return { messages, streaming, error, send, stop, clear }
}
