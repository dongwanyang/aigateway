import { useEffect, useRef } from 'react'
import type { ChatPageMessage } from '@/types'
import MessageBubble from './MessageBubble'

interface ChatTimelineProps {
  messages: ChatPageMessage[]
  streaming: boolean
  streamingId: string | null
}

export default function ChatTimeline({ messages, streaming, streamingId }: ChatTimelineProps) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  // 用户是否贴近底部(距底 < 120px 视为"在底部")。只有贴近底部时才自动跟随,
  // 否则用户主动上滚回看历史时,新 token 不会把视图拽下去。
  const atBottomRef = useRef(true)

  const handleScroll = () => {
    const el = scrollRef.current
    if (!el) return
    const dist = el.scrollHeight - el.scrollTop - el.clientHeight
    atBottomRef.current = dist < 120
  }

  useEffect(() => {
    // 流式期间每个 token 都会触发本 effect;若仍用 'smooth',多个平滑动画互相
    // 抢占 → 卡顿。流式时改 'auto'(瞬移),非流式(新消息/历史载入)才平滑。
    if (!atBottomRef.current) return
    bottomRef.current?.scrollIntoView({ behavior: streaming ? 'auto' : 'smooth' })
  }, [messages, streaming])

  return (
    <div
      ref={scrollRef}
      onScroll={handleScroll}
      className="flex flex-col overflow-y-auto"
      style={{ height: '100%' }}
    >
      {messages.map(m => (
        <MessageBubble
          key={m.id}
          msg={m}
          isStreaming={streaming && m.id === streamingId}
        />
      ))}
      <div ref={bottomRef} />
    </div>
  )
}
