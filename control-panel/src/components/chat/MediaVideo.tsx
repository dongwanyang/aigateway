import { useEffect, useRef, useState } from 'react'
import {
  VIDEO_POLL_TIMEOUT_MS,
  extractVideoUrl,
  isPlayableVideoUrl,
  isVideoSucceeded,
  watchVideoUntilTerminal,
} from '@/services/chatRuntime'

type Phase = 'polling' | 'succeeded' | 'failed' | 'timeout'

interface MediaVideoProps {
  content: string
  videoId?: string
  videoUrl?: string
  /** 消息状态层已确定的终态，用于挂载时直接落到终态而不重新轮询。 */
  videoPhase?: Phase
  done: boolean
}

const ELAPSED_TICK_MS = 1000

/**
 * 解析视频任务 id。要求内容包含 "id=<vid>" 且后续出现 "poll /v1/videos/"，
 * 避免把普通文本中的 "id=xxx" 误判为视频任务。
 */
export function parseVideoId(content: string): string | null {
  const m = content.match(/id=([\w-]+).*poll\s+\/v1\/videos\//)
  return m ? m[1] : null
}

export { extractVideoUrl }

export default function MediaVideo({
  content,
  videoId: initialVideoId,
  videoUrl: initialVideoUrl,
  videoPhase: initialPhase,
  done,
}: MediaVideoProps) {
  const videoId = done ? (initialVideoId ?? parseVideoId(content)) : null
  // 已知 URL 必须直接渲染。之前只把 URL 写进 state 而不推进 phase，
  // 于是外层轮询拿到结果、或刷新后恢复已完成的消息时，phase 仍停在 polling，
  // 渲染条件 phase === 'succeeded' 永远不成立，视频始终显示为"生成中"。
  const resolvedPhase: Phase | null = initialVideoUrl
    ? 'succeeded'
    : (initialPhase && initialPhase !== 'polling' ? initialPhase : null)

  const [phase, setPhase] = useState<Phase>(resolvedPhase ?? 'polling')
  const [videoUrl, setVideoUrl] = useState<string | null>(initialVideoUrl ?? null)
  const [elapsed, setElapsed] = useState(0)
  const mountedRef = useRef(true)
  useEffect(() => () => { mountedRef.current = false }, [])

  useEffect(() => {
    if (!initialVideoUrl) return
    setVideoUrl(initialVideoUrl)
    setPhase('succeeded')
  }, [initialVideoUrl])

  useEffect(() => {
    if (resolvedPhase) setPhase(resolvedPhase)
  }, [resolvedPhase])

  useEffect(() => {
    if (!done || !videoId || resolvedPhase) return
    setPhase('polling')
    setElapsed(0)

    // 共享轮询：同一个 videoId 全局只有一条循环，与消息状态层复用同一结果，
    // 不再出现两个轮询器各自超时/各自解析的分叉。
    const watch = watchVideoUntilTerminal(videoId)
    const start = Date.now()
    const ticker = setInterval(() => {
      if (mountedRef.current) setElapsed(Date.now() - start)
    }, ELAPSED_TICK_MS)

    void watch.result.then(outcome => {
      if (!mountedRef.current) return
      if (outcome.kind === 'cancelled') return
      if (outcome.kind === 'timeout') {
        setPhase('timeout')
        return
      }
      const url = extractVideoUrl(outcome.status)
      if (isVideoSucceeded(outcome.status) && url) {
        setVideoUrl(url)
        setPhase('succeeded')
        return
      }
      setPhase('failed')
    })

    return () => {
      clearInterval(ticker)
      watch.release()
    }
  }, [done, videoId, resolvedPhase])

  if (!done) {
    return (
      <div className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
        <span className="animate-pulse">🎬 提交视频任务中…</span>
      </div>
    )
  }

  if (!videoId) {
    return <div className="text-sm" style={{ color: 'var(--color-danger)' }}>无法解析视频任务 id</div>
  }

  if (phase === 'succeeded' && videoUrl) {
    if (isPlayableVideoUrl(videoUrl)) {
      return (
        <video
          src={videoUrl}
          controls
          className="max-w-full rounded-md"
          style={{ maxHeight: '400px', border: '1px solid var(--color-border)' }}
        />
      )
    }
    return (
      <div className="text-sm" style={{ color: 'var(--color-danger)' }}>
        视频 URL 无效
      </div>
    )
  }

  if (phase === 'failed') {
    return (
      <div className="text-sm" style={{ color: 'var(--color-danger)' }}>
        视频生成失败
      </div>
    )
  }

  if (phase === 'timeout') {
    return (
      <div className="text-sm" style={{ color: 'var(--color-danger)' }}>
        视频生成超时({Math.round(VIDEO_POLL_TIMEOUT_MS / 1000)}s)
      </div>
    )
  }

  return (
    <div className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
      <span className="animate-pulse">🎬 生成视频中…</span>
      <span className="ml-2" style={{ opacity: 0.7 }}>{Math.round(elapsed / 1000)}s</span>
    </div>
  )
}
