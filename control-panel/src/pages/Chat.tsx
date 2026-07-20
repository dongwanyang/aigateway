import { getSavedApiKey } from '@/api/client'
import { useChatSessions } from '@/hooks/useChatSessions'
import SessionList from '@/components/chat/SessionList'
import ChatTimeline from '@/components/chat/ChatTimeline'
import ChatComposer from '@/components/chat/ChatComposer'
import { Trash2 } from 'lucide-react'

export default function Chat() {
  const {
    sessions, activeId, active, streaming, error, pendingAssistantId,
    newSession, selectSession, deleteSession,
    send, stop, clearActive,
    confirmDraftMsg, rejectDraftMsg,
  } = useChatSessions()
  const hasKey = !!getSavedApiKey()

  // 聊天区高度:视口 - 56px 顶栏 - 48px(上下 padding,Layout main padding 24*2)
  const chatHeight = 'calc(100vh - 56px - 48px)'

  if (!hasKey) {
    return (
      <div className="flex items-center justify-center" style={{ height: chatHeight }}>
        <div className="text-center" style={{ color: 'var(--color-text-secondary)' }}>
          <p className="mb-2">请先在任一页面设置 API Key(右上角 / 其他页面输入框)。</p>
          <p className="text-sm" style={{ opacity: 0.7 }}>设置后回到本页即可开始聊天。</p>
        </div>
      </div>
    )
  }

  const messages = active?.messages ?? []
  const lastAssistant = [...messages].reverse().find(m => m.role === 'assistant')
  const streamingId = streaming ? (lastAssistant?.id ?? null) : null

  return (
    <div className="flex" style={{ height: chatHeight }}>
      {/* 会话列表(二级侧栏) */}
      <SessionList
        sessions={sessions}
        activeId={activeId}
        onNew={newSession}
        onSelect={selectSession}
        onDelete={deleteSession}
      />
      {/* 聊天主区 */}
      <div className="flex flex-col flex-1 min-w-0 pl-3">
        <div className="flex items-center justify-between px-1 py-2">
          <h2 className="text-md font-semibold" style={{ color: 'var(--color-text-primary)' }}>
            {active?.title || '聊天'}
          </h2>
          <button
            onClick={clearActive}
            disabled={streaming || messages.length === 0}
            className="flex items-center gap-1 px-2 py-1 rounded-md text-sm cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
            style={{ color: 'var(--color-text-secondary)' }}
          >
            <Trash2 size={14} /> 清空
          </button>
        </div>
        {error && (
          <div className="mx-1 mb-2 px-3 py-2 rounded-md text-sm" style={{ backgroundColor: 'var(--color-danger)', color: '#fff' }}>
            {error}
          </div>
        )}
        <div className="flex-1 min-h-0 mx-1 rounded-md" style={{ border: '1px solid var(--color-border)', backgroundColor: 'var(--color-bg-base)' }}>
          <ChatTimeline
            messages={messages}
            streaming={streaming}
            streamingId={streamingId}
            pendingAssistantId={pendingAssistantId}
            onConfirmDraft={confirmDraftMsg}
            onRejectDraft={rejectDraftMsg}
          />
        </div>
        <div className="mx-1 mt-2 rounded-md" style={{ backgroundColor: 'var(--color-bg-elevated)' }}>
          <ChatComposer streaming={streaming} disabled={false} onSend={send} onStop={stop} />
        </div>
      </div>
    </div>
  )
}
