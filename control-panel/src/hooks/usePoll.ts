import { useState, useCallback, useEffect, useRef } from 'react'

interface PollOptions {
  intervalMs: number
  backoffIntervalMs: number
  maxConsecutiveErrors: number
  pauseOnHidden: boolean
}

const DEFAULT_OPTIONS: PollOptions = {
  intervalMs: 10000,
  backoffIntervalMs: 30000,
  maxConsecutiveErrors: 3,
  pauseOnHidden: true,
}

/**
 * Visibility-aware polling hook with exponential backoff on errors.
 *
 * Features:
 * - Pauses polling when page is hidden (Page Visibility API)
 * - Switches to backoff interval after consecutive errors
 * - Tracks last updated timestamp
 * - Supports manual refetch
 */
export function usePoll<T>(
  fn: () => Promise<T>,
  optionsOrInterval: Partial<PollOptions> | number = {},
) {
  const options: PollOptions = typeof optionsOrInterval === 'number'
    ? { ...DEFAULT_OPTIONS, intervalMs: optionsOrInterval }
    : { ...DEFAULT_OPTIONS, ...optionsOrInterval }

  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<Error | null>(null)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const [consecutiveErrors, setConsecutiveErrors] = useState(0)

  const fnRef = useRef(fn)
  fnRef.current = fn

  const optionsRef = useRef(options)
  optionsRef.current = options

  const consecutiveErrorsRef = useRef(0)
  const isVisibleRef = useRef(!document.hidden)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const refetch = useCallback(async () => {
    try {
      setLoading(true)
      const result = await fnRef.current()
      setData(result)
      setError(null)
      setLastUpdated(new Date())
      consecutiveErrorsRef.current = 0
      setConsecutiveErrors(0)
    } catch (e) {
      const err = e instanceof Error ? e : new Error(String(e))
      setError(err)
      consecutiveErrorsRef.current += 1
      setConsecutiveErrors(consecutiveErrorsRef.current)
    } finally {
      setLoading(false)
    }
  }, [])

  // Schedule next poll based on current state
  const scheduleNext = useCallback(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current)
    }

    const opts = optionsRef.current
    // Use backoff interval if consecutive errors exceed threshold
    const interval = consecutiveErrorsRef.current >= opts.maxConsecutiveErrors
      ? opts.backoffIntervalMs
      : opts.intervalMs

    timerRef.current = setTimeout(async () => {
      // Skip if page is hidden and pauseOnHidden is enabled
      if (opts.pauseOnHidden && !isVisibleRef.current) {
        scheduleNext()
        return
      }
      await refetch()
      scheduleNext()
    }, interval)
  }, [refetch])

  // Initial fetch + start polling
  useEffect(() => {
    refetch().then(() => scheduleNext())

    return () => {
      if (timerRef.current) {
        clearTimeout(timerRef.current)
      }
    }
  }, [refetch, scheduleNext])

  // Page Visibility API integration
  useEffect(() => {
    if (!optionsRef.current.pauseOnHidden) return

    const handleVisibilityChange = () => {
      isVisibleRef.current = !document.hidden
      // When page becomes visible again, immediately refetch
      if (!document.hidden) {
        refetch().then(() => scheduleNext())
      }
    }

    document.addEventListener('visibilitychange', handleVisibilityChange)
    return () => {
      document.removeEventListener('visibilitychange', handleVisibilityChange)
    }
  }, [refetch, scheduleNext])

  return { data, loading, error, lastUpdated, consecutiveErrors, refetch }
}
