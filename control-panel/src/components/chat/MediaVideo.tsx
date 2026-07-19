import { useEffect, useRef, useState } from 'react'
import { getVideoStatus } from '@/api/client'

type Phase = 'idle' | 'polling' | 'succeeded' | 'failed' | 'timeout'

interface MediaVideoProps {
  content: string
  done: boolean
}

const POLL_INTERVAL_MS = 3000
const TIMEOUT_MS = 120000

/**
 * 解析视频任务 id。要求内容包含 "id=<vid>" 且后续出现 "poll /v1/videos/"，
 * 避免把普通文本中的 "id=xxx" 误判为视频任务。
 */
function parseVideoId(content: string): string | null {
  const m = content.match(/id=([\w-]+).*poll\s+\/v1\/videos\//)
  return m ? m[1] : null
}

export default function MediaVideo({ content, done }: MediaVideoProps) {
  const [phase, setPhase] = useState<Phase>('idle')
  const [videoUrl, setVideoUrl] = useState<string | null>(null)
  const [elapsed, setElapsed] = useState(0)
  // 用 setTimeout 递归替代 setInterval，防止 poll() 耗时 >3s 时多个 HTTP 请求并发。
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const isPollingRef = useRef(false)
  // mounted ref: cancelled 只阻止 poll() 继续执行,但不防止已 resolve 的 setState
  // 在 cancelled→unmount 之间的窗口期调用。用 ref 确保 unmount 后 setState 被丢弃。
  const mountedRef = useRef(true)
  useEffect(() => () => { mountedRef.current = false }, [])

  const videoId = done ? parseVideoId(content) : null

  useEffect(() => {
    if (!done || !videoId) return
    setPhase('polling')
    setElapsed(0)
    let cancelled = false
    const start = Date.now()

    async function poll() {
      if (cancelled || !mountedRef.current) return
      if (isPollingRef.current) {
        // 上一次 poll 还在进行中（网络慢/服务端响应慢），跳过本轮，
        // 等上一次 poll 完成后再重新调度。
        return
      }
      isPollingRef.current = true

      try {
        const e = Date.now() - start
        if (mountedRef.current) setElapsed(e)
        if (e > TIMEOUT_MS) {
          if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null }
          if (mountedRef.current) setPhase('timeout')
          return
        }
        const st = await getVideoStatus(videoId!)
        if (cancelled) return
        if (st.status === 'succeeded' && st.video?.url) {
          if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null }
          if (mountedRef.current) {
            setVideoUrl(st.video.url)
            setPhase('succeeded')
          }
          return
        }
        if (st.status === 'failed' || st.error) {
          if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null }
          if (mountedRef.current) setPhase('failed')
          return
        }
        // queued / in_progress → 继续轮询
      } catch {
        if (cancelled) return
        // 单次查询失败,不立即终止,下一轮重试(直到超时)
      } finally {
        isPollingRef.current = false
      }

      // 轮询继续：只在 timer 还存在时调度下一轮
      if (!cancelled && timerRef.current !== null) {
        scheduleNext()
      }
    }

    function scheduleNext() {
      if (timerRef.current) clearTimeout(timerRef.current)
      timerRef.current = setTimeout(poll, POLL_INTERVAL_MS)
    }

    scheduleNext()
    return () => {
      cancelled = true
      if (timerRef.current) clearTimeout(timerRef.current)
    }
  }, [done, videoId])

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
    // 防御纵深：只接受 data: 或 http(s):// URL，防止恶意上游返回 javascript: 等危险协议。
    // <video> 标签本身会阻止 javascript: 执行，但验证是好的安全习惯。
    const isValid = videoUrl.startsWith('data:') || /^https?:\/\//i.test(videoUrl)
    if (isValid) {
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
        视频生成超时({Math.round(TIMEOUT_MS / 1000)}s)
      </div>
    )
  }

  // polling
  return (
    <div className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
      <span className="animate-pulse">🎬 生成视频中…</span>
      <span className="ml-2" style={{ opacity: 0.7 }}>{Math.round(elapsed / 1000)}s</span>
    </div>
  )
}
