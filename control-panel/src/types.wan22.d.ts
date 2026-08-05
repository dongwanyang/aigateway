import '@/types'

declare module '@/types' {
  interface GenerationOptions {
    source_draft_id?: string
  }

  interface ChatPageMessage {
    /** Stable server request identity used for cancellation and refresh recovery. */
    generationRequestId?: string
  }

  interface HealthData {
    commit_sha?: string
    image_version?: string
  }
}

export {}
