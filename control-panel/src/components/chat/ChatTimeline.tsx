import { useEffect, useRef } from 'react'
import type { ChatPageMessage } from '@/types'
import MessageBubble from './MessageBubble'

interface ChatTimelineProps {
  messages: ChatPageMessage[]
  streaming: boolean
  streamingId: string | null
  pendingAssistantId: string | null
  onConfirmDraft?: (msgId: string) => void
  onRejectDraft?: (msgId: string) => void
  onCreateVideoFromDraft?: (msgId: string) => void
}

export default function ChatTimeline({
  messages,
  streaming,
  streamingId,
  pendingAssistantId,
  onConfirmDraft,
  onRejectDraft,
  onCreateVideoFromDraft,
}: ChatTimelineProps) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const atBottomRef = useRef(true)

  const handleScroll = () => {
    const el = scrollRef.current
    if (!el) return
    const dist = el.scrollHeight - el.scrollTop - el.clientHeight
    atBottomRef.current = dist < 120
  }

  useEffect(() => {
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
      {messages.map(message => (
        <MessageBubble
          key={message.id}
          msg={message}
          isStreaming={streaming && message.id === streamingId}
          pendingAssistantId={pendingAssistantId}
          onConfirmDraft={onConfirmDraft}
          onRejectDraft={onRejectDraft}
          onCreateVideoFromDraft={onCreateVideoFromDraft}
        />
      ))}
      <div ref={bottomRef} />
    </div>
  )
}
