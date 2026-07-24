import type { ChatDraftState } from '@/types'
import { Check, RefreshCw, Loader2, AlertTriangle } from 'lucide-react'
import ImageLightbox from './ImageLightbox'

interface DraftCardProps {
  draft: ChatDraftState
  onConfirm: () => void
  onReject: () => void
}

/** 草稿预览/确认/拒绝卡片。挂在 generation 意图助手消息上。 */
export default function DraftCard({ draft, onConfirm, onReject }: DraftCardProps) {
  const busy = draft.status === 'generating' || draft.status === 'confirming' || draft.status === 'rejecting'
  const terminal = draft.status === 'expired' || draft.status === 'error'

  return (
    <div className="flex flex-col gap-2" style={{ minWidth: 220 }}>
      {/* 状态文案 */}
      {draft.status === 'generating' && (
        <span className="text-xs flex items-center gap-1" style={{ color: 'var(--color-text-secondary)' }}>
          <Loader2 size={12} className="animate-spin" /> 正在生成草稿预览…
        </span>
      )}
      {draft.status === 'pending' && (
        <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
          草稿预览({draft.mediaType === 'video' ? '视频' : '图片'})· 确认后高清放大
        </span>
      )}
      {draft.status === 'confirming' && (
        <span className="text-xs flex items-center gap-1" style={{ color: 'var(--color-text-secondary)' }}>
          <Loader2 size={12} className="animate-spin" /> 正在高清放大…
        </span>
      )}
      {draft.status === 'rejecting' && (
        <span className="text-xs flex items-center gap-1" style={{ color: 'var(--color-text-secondary)' }}>
          <Loader2 size={12} className="animate-spin" /> 正在重新生成草稿…
        </span>
      )}
      {draft.status === 'confirmed' && (
        <span className="text-xs" style={{ color: 'var(--color-success, #16a34a)' }}>
          {draft.resultLost
            ? '✓ 已确认 · 刷新后仅保留预览(高清图未缓存,需重新生成)'
            : `✓ 已确认 · ${draft.resultDataUrl ? '高清图已生成' : ''}`}
        </span>
      )}
      {(terminal) && (
        <span className="text-xs flex items-center gap-1" style={{ color: 'var(--color-danger)' }}>
          <AlertTriangle size={12} /> {draft.status === 'expired' ? '草稿已过期' : '操作失败'}
          {draft.errorMessage ? `:${draft.errorMessage}` : ''}
        </span>
      )}

      {/* 预览图(确认前)/ 高清图(确认后)。图片可点击放大查看 4K 细节。 */}
      {draft.status === 'confirmed' && draft.resultDataUrl ? (
        <ImageLightbox src={draft.resultDataUrl} alt="高清结果" thumbAlt="高清结果(点击放大)" />
      ) : draft.previewDataUrl ? (
        draft.mediaType === 'video' ? (
          <video
            src={draft.previewDataUrl}
            controls
            style={{ maxWidth: '100%', borderRadius: 6, border: '1px solid var(--color-border)' }}
          />
        ) : (
          <ImageLightbox src={draft.previewDataUrl} alt="草稿预览" thumbAlt="草稿预览(点击放大)" />
        )
      ) : !terminal ? (
        <div
          className="flex items-center justify-center"
          style={{ height: 120, color: 'var(--color-text-secondary)', backgroundColor: 'var(--color-bg-overlay)', borderRadius: 6 }}
        >
          <Loader2 size={20} className="animate-spin" />
        </div>
      ) : null}

      {/* 操作按钮:pending/出错时可确认/拒绝;confirming/rejecting 时禁用 */}
      <div className="flex gap-2">
        <button
          onClick={onConfirm}
          disabled={busy || draft.status === 'confirmed'}
          className="flex items-center gap-1 px-3 py-1.5 rounded-md text-sm cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
          style={{ backgroundColor: 'var(--color-primary)', color: 'var(--color-text-inverse)' }}
        >
          {draft.status === 'confirming' ? <Loader2 size={14} className="animate-spin" /> : <Check size={14} />}
          确认放大
        </button>
        <button
          onClick={onReject}
          disabled={busy}
          className="flex items-center gap-1 px-3 py-1.5 rounded-md text-sm cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
          style={{ color: 'var(--color-text-secondary)', border: '1px solid var(--color-border)', backgroundColor: 'transparent' }}
        >
          {draft.status === 'rejecting' ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}
          重新生成
        </button>
      </div>
    </div>
  )
}
