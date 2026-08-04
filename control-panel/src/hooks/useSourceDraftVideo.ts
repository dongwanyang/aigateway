import { useCallback } from 'react'
import { createVideoDraftFromSource } from '@/api/sourceDraftVideo'
import type { ChatDraftState, ChatPageMessage, ChatSession } from '@/types'
import { persistSessions, titleFromMessages } from '@/services/chatStorage'
import {
  nextMessageId,
  pollDraftUntilSettled,
} from '@/services/chatRuntime'
import { useChatStore } from '@/stores/chatStore'

interface SourceAwareDraft extends ChatDraftState {
  sourceDraftId?: string
}

export interface CreateSourceDraftVideoInput {
  sourceDraftId: string
  sourcePreviewDataUrl?: string
  motionPrompt: string
  durationSeconds: 3 | 5 | 8
  fps: number
  chatSessionId: string
}

function patchSession(
  sessions: ChatSession[],
  sessionId: string,
  updater: (messages: ChatPageMessage[]) => ChatPageMessage[],
): ChatSession[] {
  return sessions.map(session => {
    if (session.id !== sessionId) return session
    const messages = updater(session.messages)
    return {
      ...session,
      messages,
      title: session.title === '新对话' ? titleFromMessages(messages) : session.title,
      updatedAt: Date.now(),
    }
  })
}

export function useSourceDraftVideo() {
  const setSessions = useChatStore(state => state.setSessions)
  const setStreaming = useChatStore(state => state.setStreaming)
  const setError = useChatStore(state => state.setError)
  const setPendingAssistantId = useChatStore(state => state.setPendingAssistantId)

  return useCallback(async (input: CreateSourceDraftVideoInput): Promise<void> => {
    const motionPrompt = input.motionPrompt.trim()
    if (!motionPrompt || useChatStore.getState().streaming) return

    const userId = nextMessageId()
    const assistantId = nextMessageId()
    const now = Date.now()
    const userMessage: ChatPageMessage = {
      id: userId,
      role: 'user',
      content: motionPrompt,
      referenceImageDataUrl: input.sourcePreviewDataUrl,
      referenceImageName: '已生成图片',
      ts: now,
    }
    const assistantMessage: ChatPageMessage = {
      id: assistantId,
      role: 'assistant',
      content: '',
      intent: 'generation:video',
      model: 'source-draft',
      awaitingDraft: true,
      awaitingDraftSince: now,
      ts: now,
    }

    setError(null)
    setStreaming(true)
    setPendingAssistantId(assistantId)
    setSessions(previous => patchSession(
      previous,
      input.chatSessionId,
      messages => [...messages, userMessage, assistantMessage],
    ))

    const patchAssistant = (
      updater: (message: ChatPageMessage) => ChatPageMessage,
    ) => {
      setSessions(previous => patchSession(
        previous,
        input.chatSessionId,
        messages => messages.map(message => (
          message.id === assistantId ? updater(message) : message
        )),
      ))
    }

    try {
      const created = await createVideoDraftFromSource(input.sourceDraftId, {
        motionPrompt,
        durationSeconds: input.durationSeconds,
        fps: input.fps,
        chatSessionId: input.chatSessionId,
      })
      const draft: SourceAwareDraft = {
        draftId: created.draft_id,
        previewUrl: created.preview_url,
        mediaType: 'video',
        status: 'generating',
        stage: 'preview_ready',
        progress: 1,
        progressSource: 'complete',
        sourceDraftId: input.sourceDraftId,
      }
      patchAssistant(message => ({
        ...message,
        draft,
        awaitingDraft: true,
        awaitingDraftSince: Date.now(),
      }))
      setPendingAssistantId(null)

      const result = await pollDraftUntilSettled(created.draft_id, progress => {
        patchAssistant(message => {
          if (!message.draft || message.draft.draftId !== created.draft_id) return message
          const nextDraft = message.draft as SourceAwareDraft
          return {
            ...message,
            draft: {
              ...nextDraft,
              status: progress.status === 'pending' ? 'pending' : 'generating',
              stage: progress.stage ?? nextDraft.stage,
              progress: typeof progress.progress === 'number'
                ? Math.min(1, Math.max(0, progress.progress))
                : nextDraft.progress,
              progressSource: progress.progressSource ?? nextDraft.progressSource,
            },
          }
        })
      })

      if (result.kind === 'ready') {
        patchAssistant(message => {
          if (!message.draft) return message
          return {
            ...message,
            draft: {
              ...(message.draft as SourceAwareDraft),
              status: 'pending',
              stage: 'preview_ready',
              progress: 1,
              progressSource: 'complete',
              previewDataUrl: result.previewDataUrl,
              errorMessage: undefined,
            },
            awaitingDraft: false,
          }
        })
      } else if (result.kind !== 'duplicate') {
        patchAssistant(message => ({
          ...message,
          draft: message.draft ? {
            ...(message.draft as SourceAwareDraft),
            status: result.kind,
            errorMessage: result.message,
          } : message.draft,
          awaitingDraft: false,
        }))
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : '创建视频草稿失败'
      setError(message)
      patchAssistant(current => ({
        ...current,
        content: message,
        error: true,
        awaitingDraft: false,
      }))
    } finally {
      setPendingAssistantId(null)
      setStreaming(false)
      try { persistSessions(useChatStore.getState().sessions) } catch { /* ignore */ }
    }
  }, [setError, setPendingAssistantId, setSessions, setStreaming])
}
