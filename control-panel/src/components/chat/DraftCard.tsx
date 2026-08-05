import type { ChatDraftState } from '@/types'
import { Check, RefreshCw, Loader2, AlertTriangle, Video } from 'lucide-react'
import ImageLightbox from './ImageLightbox'

interface SourceAwareDraft extends ChatDraftState {
  sourceDraftId?: string
}

interface DraftCardProps {
  draft: ChatDraftState
  onConfirm: () => void
  onReject: () => void
  onCreateVideo?: () => void
}

/** 草稿预览/确认/拒绝卡片。挂在 generation 意图助手消息上。 */
export default function DraftCard({ draft, onConfirm, onReject, onCreateVideo }: DraftCardProps) {
  const sourceDraftId = (draft as SourceAwareDraft).sourceDraftId
  const busy = ['queued', 'running', 'generating', 'refining', 'confirming', 'rejecting'].includes(draft.status)
  const completed = ['confirmed', 'completed'].includes(draft.status)
  const terminal = draft.status === 'expired' || draft.status === 'error' || draft.status === 'cancelled'
  const progressPercent = typeof draft.progress === 'number'
    ? Math.round(Math.min(1, Math.max(0, draft.progress)) * 100)
    : null
  const hasRealComfyProgress = draft.progressSource === 'comfyui'
  const indeterminateProgress = busy
    && progressPercent !== null
    && ['running', 'refining', 'confirming'].includes(draft.status)
    && !hasRealComfyProgress
  const progressText = (!indeterminateProgress && progressPercent !== null && hasRealComfyProgress)
    ? ` ${progressPercent}%`
    : ''
  const stageText = (() => {
    if (!draft.stage || draft.stage === draft.status) return ''
    const sampling = draft.stage.match(/^sampling (\d+)\/(\d+)$/)
    if (sampling) return `采样 ${sampling[1]}/${sampling[2]}`
    const executing = draft.stage.match(/^executing (.+)$/)
    if (executing) return `ComfyUI 节点 ${executing[1]} 执行中`
    const labels: Record<string, string> = {
      waiting_for_comfyui: '等待 ComfyUI 响应',
      preparing_for_comfyui: '正在准备 ComfyUI 工作流',
      finalizing: '正在解码并保存',
      downloading: '正在获取生成结果',
      preview_ready: '预览已就绪',
    }
    return labels[draft.stage] ?? draft.stage
  })()
  const stageLabel = stageText ? ` · ${stageText}` : ''
  const canCreateVideo = draft.mediaType === 'image'
    && completed
    && !draft.resultLost
    && Boolean(onCreateVideo)
  const showDraftActions = !['expired', 'cancelled'].includes(draft.status)

  return (
    <div className="flex flex-col gap-2" style={{ minWidth: 220 }}>
      {['queued', 'running', 'generating'].includes(draft.status) && (
        <span className="text-xs flex items-center gap-1" style={{ color: 'var(--color-text-secondary)' }}>
          <Loader2 size={12} className="animate-spin" /> {draft.status === 'queued' ? 'ComfyUI 队列等待中…' : 'ComfyUI 正在生成草稿预览…'}
          {progressText}
          {stageLabel}
        </span>
      )}
      {draft.status === 'pending' && (
        <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
          {draft.mediaType === 'video'
            ? sourceDraftId
              ? '来源图片已冻结 · 确认后由 ComfyUI 生成视频'
              : '视频关键帧预览 · 确认后由 ComfyUI 生成本地视频'
            : '图片草稿预览 · 确认后由同一 ComfyUI 工作流生成高清结果'}
        </span>
      )}
      {(draft.status === 'confirming' || draft.status === 'refining') && (
        <span className="text-xs flex items-center gap-1" style={{ color: 'var(--color-text-secondary)' }}>
          <Loader2 size={12} className="animate-spin" /> {draft.mediaType === 'video' ? 'ComfyUI 正在生成视频…' : 'ComfyUI 正在精修高清图…'}
          {progressText}
          {stageLabel}
        </span>
      )}
      {draft.status === 'rejecting' && (
        <span className="text-xs flex items-center gap-1" style={{ color: 'var(--color-text-secondary)' }}>
          <Loader2 size={12} className="animate-spin" /> 正在重新生成草稿…
        </span>
      )}
      {completed && (
        <span className="text-xs" style={{ color: 'var(--color-success, #16a34a)' }}>
          {draft.resultLost
            ? `✓ 已确认 · 刷新后仅保留预览(${draft.mediaType === 'video' ? '视频' : '高清图'}未缓存,需重新生成)`
            : `✓ 已确认 · ${draft.resultDataUrl ? (draft.mediaType === 'video' ? '视频已生成' : '高清图已生成') : ''}`}
        </span>
      )}
      {terminal && (
        <span className="text-xs flex items-center gap-1" style={{ color: 'var(--color-danger)' }}>
          <AlertTriangle size={12} /> {draft.status === 'expired' ? '草稿已过期' : '操作失败'}
          {draft.errorMessage ? `:${draft.errorMessage}` : ''}
        </span>
      )}
      {busy && progressPercent !== null && (
        <div
          aria-label="草稿生成进度"
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={indeterminateProgress ? undefined : progressPercent}
          style={{ height: 4, borderRadius: 999, overflow: 'hidden', backgroundColor: 'var(--color-bg-overlay)' }}
        >
          <div
            className={indeterminateProgress ? 'animate-pulse' : undefined}
            style={{
              height: '100%',
              width: indeterminateProgress ? '36%' : `${progressPercent}%`,
              backgroundColor: 'var(--color-primary)',
              transition: 'width 180ms ease',
            }}
          />
        </div>
      )}

      {completed && draft.resultDataUrl ? (
        draft.mediaType === 'video' ? (
          <video src={draft.resultDataUrl} controls playsInline className="max-w-full rounded-md" />
        ) : (
          <ImageLightbox src={draft.resultDataUrl} alt="高清结果" thumbAlt="高清结果(点击放大)" />
        )
      ) : draft.previewDataUrl ? (
        <ImageLightbox
          src={draft.previewDataUrl}
          alt={draft.mediaType === 'video' ? '视频首帧预览' : '草稿预览'}
          thumbAlt={draft.mediaType === 'video' ? '视频首帧预览(点击放大)' : '草稿预览(点击放大)'}
        />
      ) : !terminal ? (
        <div
          className="flex items-center justify-center"
          style={{ height: 120, color: 'var(--color-text-secondary)', backgroundColor: 'var(--color-bg-overlay)', borderRadius: 6 }}
        >
          <Loader2 size={20} className="animate-spin" />
        </div>
      ) : null}

      {canCreateVideo && (
        <button
          type="button"
          onClick={onCreateVideo}
          className="flex items-center justify-center gap-1 px-3 py-1.5 rounded-md text-sm cursor-pointer"
          style={{ color: 'var(--color-primary)', border: '1px solid var(--color-primary)', backgroundColor: 'transparent' }}
        >
          <Video size={14} /> 基于此图生成视频
        </button>
      )}

      {showDraftActions && (
        <div className="flex gap-2">
          <button
            onClick={onConfirm}
            disabled={busy || completed}
            className="flex items-center gap-1 px-3 py-1.5 rounded-md text-sm cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
            style={{ backgroundColor: 'var(--color-primary)', color: 'var(--color-text-inverse)' }}
          >
            {(draft.status === 'confirming' || draft.status === 'refining') ? <Loader2 size={14} className="animate-spin" /> : <Check size={14} />}
            {draft.mediaType === 'video' ? '确认生成视频' : '确认生成高清图'}
          </button>
          {!sourceDraftId && !completed && (
            <button
              onClick={onReject}
              disabled={busy}
              className="flex items-center gap-1 px-3 py-1.5 rounded-md text-sm cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
              style={{ color: 'var(--color-text-secondary)', border: '1px solid var(--color-border)', backgroundColor: 'transparent' }}
            >
              {draft.status === 'rejecting' ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}
              重新生成
            </button>
          )}
        </div>
      )}
    </div>
  )
}
