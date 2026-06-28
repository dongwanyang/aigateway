import { useState, useCallback } from 'react'

/** 轮询 hook — 定期拉取数据 */
export function usePoll<T>(fn: () => Promise<T>, intervalMs: number = 10000) {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<Error | null>(null)

  const refetch = useCallback(async () => {
    try {
      setLoading(true)
      const result = await fn()
      setData(result)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e : new Error(String(e)))
    } finally {
      setLoading(false)
    }
  }, [fn])

  useState(() => {
    refetch()
    const timer = setInterval(refetch, intervalMs)
    return () => clearInterval(timer)
  })

  return { data, loading, error, refetch }
}
