import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { ChatDraftState } from '@/types'
import DraftCard from './DraftCard'

function imageResultDraft(): ChatDraftState {
  return {
    draftId: 'image-draft',
    previewUrl: '/admin/draft/image-draft/preview',
    mediaType: 'image',
    status: 'completed',
    resultDataUrl: 'data:image/png;base64,aW1hZ2U=',
  }
}

describe('DraftCard existing image to video flow', () => {
  it('offers the action only for reusable completed image results', () => {
    const onCreateVideo = vi.fn()
    render(
      <DraftCard
        draft={imageResultDraft()}
        onConfirm={vi.fn()}
        onReject={vi.fn()}
        onCreateVideo={onCreateVideo}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: '基于此图生成视频' }))
    expect(onCreateVideo).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('button', { name: '确认生成高清图' })).not.toBeInTheDocument()
  })

  it('keeps a source-derived video immutable in the UI', () => {
    const draft = {
      draftId: 'video-draft',
      previewUrl: '/admin/draft/video-draft/preview',
      mediaType: 'video',
      status: 'pending',
      previewDataUrl: 'data:image/png;base64,a2V5ZnJhbWU=',
      sourceDraftId: 'image-draft',
    } as ChatDraftState & { sourceDraftId: string }

    render(
      <DraftCard
        draft={draft}
        onConfirm={vi.fn()}
        onReject={vi.fn()}
      />,
    )

    expect(screen.getByText('来源图片已冻结 · 确认后由 ComfyUI 生成视频')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '确认生成视频' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '重新生成' })).not.toBeInTheDocument()
  })
})
