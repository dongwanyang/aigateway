import { useEffect, useState } from 'react'
import { useChatSessions } from '@/hooks/useChatSessions'
import { useAuth } from '@/contexts/AuthContext'
import { useQuery } from '@tanstack/react-query'
import { getGenerationPresets } from '@/api/client'
import { queryKeys } from '@/query/keys'
import {
  cancelAllSessionGenerations,
  cancelLatestSessionGeneration,
} from '@/services/cancelSessionGeneration'
import SessionList from '@/components/chat/SessionList'
import ChatTimeline from '@/components/chat/ChatTimeline'
import ChatComposer from '@/components/chat/ChatComposer'
import type { ChatPageMessage, ChatReferenceImage, GenerationOptions } from '@/types'
import {
  DEFAULT_VIDEO_DURATION_SECONDS,
  DEFAULT_VIDEO_FPS,
} from '@/types/videoGeneration'
import { Trash2, Video, X } from 'lucide-react'

interface SelectedVideoSource {
  draftId: string
  previewDataUrl?: string
}

type SourceAwareGenerationOptions = GenerationOptions & {
  source_draft_id?: string
}

const CANCELLABLE_DRAFT_STATUSES = new Set([
  'queued',
  'running',
  'generating',
  'confirming',
  'refining',
])

function hasCancellableGeneration(message: ChatPageMessage): boolean {
  if (!message.generationRequestId) return false
  return Boolean(
    message.awaitingDraft
    || (message.draft && CANCELLABLE_DRAFT_STATUSES.has(message.draft.status)),
  )
}

export default function Chat() {
  const { isAuthenticated } = useAuth()
  const presetsQuery = useQuery({
    queryKey: queryKeys.generation.presets,
    queryFn: async () => (await getGenerationPresets()).data,
    enabled: isAuthenticated,
    staleTime: 30_000,
  })
  const {
    sessions, activeId, active, streaming, error, pendingAssistantId,
    newSession, selectSession, deleteSession,
    send, stop, clearActive,
    confirmDraftMsg, rejectDraftMsg,
  } = useChatSessions()
  const [selectedVideoSource, setSelectedVideoSource] = useState<SelectedVideoSource | null>(null)
  const chatHeight = 'calc(100vh - 56px - 48px)'

  useEffect(() => {
    setSelectedVideoSource(null)
  }, [activeId])

  if (!isAuthenticated) {
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
  const generationBusy = streaming || messages.some(hasCancellableGeneration)
  const lastAssistant = [...messages].reverse().find(
    message => message.role === 'assistant',
  )
  const streamingId = streaming ? (lastAssistant?.id ?? null) : null

  const selectImageDraftForVideo = (messageId: string) => {
    const message = messages.find(item => item.id === messageId)
    if (
      !message?.draft
      || message.draft.mediaType !== 'image'
      || !['confirmed', 'completed'].includes(message.draft.status)
      || message.draft.resultLost
    ) return
    setSelectedVideoSource({
      draftId: message.draft.draftId,
      previewDataUrl: message.draft.previewDataUrl ?? message.draft.resultDataUrl,
    })
  }

  const handleSend = (
    text: string,
    opts?: {
      generationOptions?: GenerationOptions
      referenceImage?: ChatReferenceImage
    },
  ) => {
    if (selectedVideoSource && activeId) {
      const source = selectedVideoSource
      setSelectedVideoSource(null)
      const sourceOptions: SourceAwareGenerationOptions = {
        backend: opts?.generationOptions?.backend ?? 'local',
        ...opts?.generationOptions,
        source_draft_id: source.draftId,
        duration_seconds: opts?.generationOptions?.duration_seconds
          ?? DEFAULT_VIDEO_DURATION_SECONDS,
        fps: opts?.generationOptions?.fps ?? DEFAULT_VIDEO_FPS,
      }
      void send(text, {
        generationOptions: sourceOptions,
      })
      return
    }
    void send(text, opts)
  }

  const handleStop = () => {
    if (streaming) {
      stop()
      return
    }
    if (activeId) void cancelLatestSessionGeneration(activeId)
  }

  const handleDeleteSession = async (sessionId: string) => {
    if (!await cancelAllSessionGenerations(sessionId)) return
    await deleteSession(sessionId)
  }

  const handleClearActive = async () => {
    if (!activeId || !await cancelAllSessionGenerations(activeId)) return
    clearActive()
  }

  return (
    <div className="flex" style={{ height: chatHeight }}>
      <SessionList
        sessions={sessions}
        activeId={activeId}
        onNew={newSession}
        onSelect={selectSession}
        onDelete={sessionId => { void handleDeleteSession(sessionId) }}
      />
      <div className="flex flex-col flex-1 min-w-0 pl-3">
        <div className="flex items-center justify-between px-1 py-2">
          <h2 className="text-md font-semibold" style={{ color: 'var(--color-text-primary)' }}>
            {active?.title || '聊天'}
          </h2>
          <button
            onClick={() => { void handleClearActive() }}
            disabled={generationBusy || messages.length === 0}
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
            onCreateVideoFromDraft={selectImageDraftForVideo}
          />
        </div>
        <div className="mx-1 mt-2 rounded-md" style={{ backgroundColor: 'var(--color-bg-elevated)' }}>
          {selectedVideoSource && (
            <div
              className="mx-3 mt-3 flex items-center gap-2 rounded-md px-3 py-2 text-xs"
              style={{ border: '1px solid var(--color-primary)', color: 'var(--color-text-secondary)' }}
            >
              {selectedVideoSource.previewDataUrl ? (
                <img
                  src={selectedVideoSource.previewDataUrl}
                  alt="视频来源图片"
                  className="h-12 w-12 rounded object-cover"
                />
              ) : (
                <Video size={18} style={{ color: 'var(--color-primary)' }} />
              )}
              <div className="flex-1">
                <div style={{ color: 'var(--color-text-primary)' }}>基于此图生成视频</div>
                <div>输入主体动作或镜头运动；当前仅“视频时长”选项生效。</div>
              </div>
              <button
                type="button"
                aria-label="取消使用此图片"
                title="取消使用此图片"
                disabled={generationBusy}
                onClick={() => setSelectedVideoSource(null)}
                className="inline-flex rounded p-1 cursor-pointer disabled:opacity-50"
              >
                <X size={14} />
              </button>
            </div>
          )}
          <ChatComposer
            streaming={generationBusy}
            disabled={false}
            sourceImageMode={Boolean(selectedVideoSource)}
            onSend={handleSend}
            onStop={handleStop}
            presets={Array.isArray(presetsQuery.data) ? presetsQuery.data : []}
            presetsLoading={presetsQuery.isLoading || presetsQuery.isFetching}
            presetsError={presetsQuery.error instanceof Error ? presetsQuery.error.message : null}
            onRefreshPresets={() => { void presetsQuery.refetch() }}
          />
        </div>
      </div>
    </div>
  )
}
