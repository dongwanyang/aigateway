export const VIDEO_DURATION_OPTIONS = [3, 5, 8] as const

export type VideoDurationSeconds = (typeof VIDEO_DURATION_OPTIONS)[number]

export const DEFAULT_VIDEO_DURATION_SECONDS: VideoDurationSeconds = 5
export const DEFAULT_VIDEO_FPS = 8
