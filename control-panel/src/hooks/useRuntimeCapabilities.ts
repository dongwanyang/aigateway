import { useCallback, useEffect, useState } from 'react'
import { getRuntimeCapabilities, getSavedApiKey } from '@/api/client'
import type { RuntimeCapabilities } from '@/api/client'

let cachedCapabilities: RuntimeCapabilities | null = null
let pendingRequest: Promise<RuntimeCapabilities> | null = null

async function loadCapabilities(): Promise<RuntimeCapabilities> {
  if (cachedCapabilities) return cachedCapabilities
  if (!pendingRequest) {
    pendingRequest = getRuntimeCapabilities()
      .then(response => {
        cachedCapabilities = response.data
        return response.data
      })
      .finally(() => {
        pendingRequest = null
      })
  }
  return pendingRequest
}

export function useRuntimeCapabilities() {
  const [data, setData] = useState<RuntimeCapabilities | null>(cachedCapabilities)
  const [loading, setLoading] = useState(Boolean(getSavedApiKey()) && !cachedCapabilities)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    if (!getSavedApiKey()) {
      setLoading(false)
      return
    }
    cachedCapabilities = null
    setLoading(true)
    setError(null)
    try {
      setData(await loadCapabilities())
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '能力状态加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!getSavedApiKey() || data) {
      setLoading(false)
      return
    }
    let cancelled = false
    void loadCapabilities()
      .then(result => {
        if (!cancelled) setData(result)
      })
      .catch(exc => {
        if (!cancelled) {
          setError(exc instanceof Error ? exc.message : '能力状态加载失败')
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [data])

  return { data, loading, error, refresh }
}

