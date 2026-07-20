import type { ChatSession } from '@/types'
import { Plus, Trash2, MessageSquare } from 'lucide-react'

interface SessionListProps {
  sessions: ChatSession[]
  activeId: string | null
  onNew: () => void
  onSelect: (id: string) => void
  onDelete: (id: string) => void
}

function relativeTime(ts: number): string {
  const diff = Date.now() - ts
  const m = Math.floor(diff / 60000)
  if (m < 1) return '刚刚'
  if (m < 60) return `${m} 分钟前`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h} 小时前`
  const d = Math.floor(h / 24)
  return `${d} 天前`
}

/** 聊天页内常驻会话列表(二级侧栏)。 */
export default function SessionList({ sessions, activeId, onNew, onSelect, onDelete }: SessionListProps) {
  return (
    <div
      className="flex flex-col"
      style={{ width: 200, borderRight: '1px solid var(--color-border)', backgroundColor: 'var(--color-bg-elevated)' }}
    >
      <div className="p-2">
        <button
          onClick={onNew}
          className="flex items-center justify-center gap-1 w-full px-2 py-2 rounded-md text-sm cursor-pointer"
          style={{ backgroundColor: 'var(--color-primary)', color: 'var(--color-text-inverse)' }}
        >
          <Plus size={16} /> 新对话
        </button>
      </div>
      <div className="flex-1 overflow-y-auto px-1 pb-2">
        {sessions.length === 0 && (
          <div className="text-xs text-center mt-4" style={{ color: 'var(--color-text-secondary)' }}>
            点击上方按钮开始
          </div>
        )}
        {sessions.map(s => {
          const isActive = s.id === activeId
          return (
            <div
              key={s.id}
              onClick={() => onSelect(s.id)}
              className="group flex items-center gap-2 px-2 py-2 rounded-md cursor-pointer mb-0.5"
              style={{
                backgroundColor: isActive ? 'var(--color-bg-overlay)' : 'transparent',
                color: 'var(--color-text-primary)',
              }}
            >
              <MessageSquare size={14} style={{ color: 'var(--color-text-secondary)', flexShrink: 0 }} />
              <div className="flex-1 min-w-0">
                <div className="text-sm truncate">{s.title || '新对话'}</div>
                <div className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                  {relativeTime(s.updatedAt)}
                </div>
              </div>
              <button
                onClick={(e) => { e.stopPropagation(); onDelete(s.id) }}
                className="opacity-0 group-hover:opacity-100 flex items-center justify-center"
                style={{ color: 'var(--color-text-secondary)', padding: 2 }}
                title="删除会话"
              >
                <Trash2 size={14} />
              </button>
            </div>
          )
        })}
      </div>
    </div>
  )
}
