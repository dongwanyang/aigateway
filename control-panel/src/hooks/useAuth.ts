import { useState, useCallback, useEffect } from 'react'
import { saveApiKey, clearApiKey, getSavedApiKey } from '@/api/client'

interface AuthState {
  apiKey: string | null
  isAuthenticated: boolean
  keyPrefix: string | null
}

/**
 * Auth persistence hook — manages API key in localStorage with auto-401 handling.
 *
 * Features:
 * - Persist API key in localStorage
 * - Auto-detect existing key on mount
 * - Show first 8 chars as prefix indicator
 * - Login/logout helpers
 */
export function useAuth() {
  const [state, setState] = useState<AuthState>(() => {
    const saved = getSavedApiKey()
    return {
      apiKey: saved,
      isAuthenticated: !!saved,
      keyPrefix: saved && saved.length >= 8 ? saved.substring(0, 8) : null,
    }
  })

  // Listen for storage changes (e.g., another tab)
  useEffect(() => {
    const handleStorage = (e: StorageEvent) => {
      if (e.key === 'aigateway_api_key') {
        const newKey = e.newValue
        setState({
          apiKey: newKey,
          isAuthenticated: !!newKey,
          keyPrefix: newKey && newKey.length >= 8 ? newKey.substring(0, 8) : null,
        })
      }
    }
    window.addEventListener('storage', handleStorage)
    return () => window.removeEventListener('storage', handleStorage)
  }, [])

  const login = useCallback((key: string) => {
    saveApiKey(key)
    setState({
      apiKey: key,
      isAuthenticated: true,
      keyPrefix: key.length >= 8 ? key.substring(0, 8) : key,
    })
  }, [])

  const logout = useCallback(() => {
    clearApiKey()
    setState({
      apiKey: null,
      isAuthenticated: false,
      keyPrefix: null,
    })
  }, [])

  return { state, login, logout }
}
