import type { ChatPageMessage } from '@/types'
import RoutingBadge from './RoutingBadge'
import MediaImage from './MediaImage'
import MediaVideo from './MediaVideo'
import DraftCard from './DraftCard'
import TypingDots from './TypingDots'

export type ContentKind = 'image' | 'video' | 'text'

/** 双保险判定:intent 或 content 形态任一命中。 */
export function classifyContent(
  intent: ChatPageMessage['intent'],
  content: string,
): ContentKind {
  if (intent === 'generation:image') return 'image'
  if (intent === 'generation:video') return 'video'
  // content 启发式兜底
  if (/^https?:\/\//i.test(content) || /^data:image\//i.test(content)) return 'image'
  if (/id=[\w-]+/.test(content) && /poll\s+\/v1\/videos\//.test(content)) return 'video'
  return 'text'
}

interface MessageBubbleProps {
  msg: ChatPageMessage
  isStreaming: boolean
  pendingAssistantId: string | null
  onConfirmDraft?: (msgId: string) => void
  onRejectDraft?: (msgId: string) => void
}

export default function MessageBubble({ msg, isStreaming, pendingAssistantId, onConfirmDraft, onRejectDraft }: MessageBubbleProps) {
  if (msg.role === 'user') {
    return (
      <div className="flex justify-end mb-4">
        <div className="max-w-[70%] px-4 py-2 rounded-lg" style={{ backgroundColor: 'var(--color-primary)', color: 'var(--color-text-inverse)' }}>
          <p className="whitespace-pre-wrap break-words">{msg.content}</p>
        </div>
      </div>
    )
  }

  // 草稿消息分支:渲染 DraftCard(不进 text/image/video 的 content 分类)
  if (msg.draft) {
    return (
      <div className="flex justify-start mb-4">
        <div
          className="max-w-[70%] px-4 py-2 rounded-lg"
          style={{
            backgroundColor: 'var(--color-bg-overlay)',
            color: 'var(--color-text-primary)',
            border: msg.error ? '1px solid var(--color-danger)' : '1px solid var(--color-border)',
          }}
        >
          <RoutingBadge intent={msg.intent} model={msg.model} />
          <DraftCard
            draft={msg.draft}
            onConfirm={() => onConfirmDraft?.(msg.id)}
            onReject={() => onRejectDraft?.(msg.id)}
          />
        </div>
      </div>
    )
  }

  const kind = classifyContent(msg.intent, msg.content)
  return (
    <div className="flex justify-start mb-4">
      <div
        className="max-w-[70%] px-4 py-2 rounded-lg"
        style={{
          backgroundColor: 'var(--color-bg-overlay)',
          color: 'var(--color-text-primary)',
          border: msg.error ? '1px solid var(--color-danger)' : '1px solid var(--color-border)',
        }}
      >
        <RoutingBadge intent={msg.intent} model={msg.model} />
        {kind === 'image' && (
          <MediaImage content={msg.content} done={!isStreaming} />
        )}
        {kind === 'video' && (
          <MediaVideo content={msg.content} done={!isStreaming} />
        )}
        {kind === 'text' && (
          <>
            {/* 空占位且 pendingAssistantId 匹配时显示三点"正在思考" */}
            {!msg.content && pendingAssistantId === msg.id ? (
              <TypingDots />
            ) : (
              <p className="whitespace-pre-wrap break-words">
                {msg.content}
                {isStreaming && <span className="animate-pulse">▌</span>}
                {!isStreaming && msg.incomplete && (
                  <span className="text-xs ml-1" style={{ color: 'var(--color-text-secondary)' }}>(已中断)</span>
                )}
                {msg.videoId && !msg.content && !isStreaming && (
                  <span className="text-xs ml-1" style={{ color: 'var(--color-text-secondary)' }}>视频生成中...</span>
                )}
              </p>
            )}
          </>
        )}
      </div>
    </div>
  )
}
