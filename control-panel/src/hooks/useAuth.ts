import { useState, useCallback, useEffect } from 'react'
import { saveApiKey, clearApiKey, getSavedApiKey } from '@/api/client'

interface AuthState {
  apiKey: string | null
  isAuthenticated: boolean
  keyPrefix: string | null
}

/**
 * Auth hook backed by an HttpOnly browser session.
 *
 * Features:
 * - Exchange API key for an HttpOnly, SameSite cookie
 * - Persist only a non-secret session marker
 * - Login/logout helpers
 */
export function useAuth() {
  const [state, setState] = useState<AuthState>(() => {
    const saved = getSavedApiKey()
    return {
      apiKey: saved,
      isAuthenticated: !!saved,
      keyPrefix: null,
    }
  })

  // Listen for storage changes (e.g., another tab)
  useEffect(() => {
    const handleStorage = (e: StorageEvent) => {
      if (e.key === 'aigateway_session_active') {
        const newKey = e.newValue
        setState({
          apiKey: newKey,
          isAuthenticated: !!newKey,
          keyPrefix: null,
        })
      }
    }
    window.addEventListener('storage', handleStorage)
    return () => window.removeEventListener('storage', handleStorage)
  }, [])

  const login = useCallback(async (key: string) => {
    await saveApiKey(key)
    setState({
      apiKey: null,
      isAuthenticated: true,
      keyPrefix: key.length >= 8 ? key.substring(0, 8) : key,
    })
  }, [])

  const logout = useCallback(async () => {
    await clearApiKey()
    setState({
      apiKey: null,
      isAuthenticated: false,
      keyPrefix: null,
    })
  }, [])

  return { state, login, logout }
}
