import { createContext, useContext, useEffect, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  clearBrowserSession,
  getBrowserSession,
  getSavedSessionMarker,
  loginWithPassword,
} from '@/api/authSession'
import { queryKeys } from '@/query/keys'
import { useAuthStore } from '@/stores/authStore'

interface AuthState {
  apiKey: null
  isAuthenticated: boolean
  keyPrefix: string | null
}

export interface AuthContextValue {
  state: AuthState
  isAuthenticated: boolean
  keyPrefix: string | null
  forceReset: boolean
  isLoading: boolean
  login: (username: string, password: string) => Promise<{ forceReset: boolean }>
  logout: () => Promise<void>
  completeForceReset: () => void
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()
  const isAuthenticated = useAuthStore(state => state.isAuthenticated)
  const keyPrefix = useAuthStore(state => state.keyPrefix)
  const forceReset = useAuthStore(state => state.forceReset)
  const setAuthenticated = useAuthStore(state => state.setAuthenticated)
  const setForceReset = useAuthStore(state => state.setForceReset)
  const clear = useAuthStore(state => state.clear)
  const sessionQuery = useQuery({
    queryKey: queryKeys.auth.session,
    queryFn: getBrowserSession,
    retry: false,
    staleTime: 30_000,
  })
  const sessionStatePending = sessionQuery.data !== undefined && (
    Boolean(sessionQuery.data.authenticated) !== isAuthenticated
    || (
      sessionQuery.data.authenticated
      && Boolean(sessionQuery.data.force_reset) !== forceReset
    )
  )

  useEffect(() => {
    if (sessionQuery.data?.authenticated) {
      setAuthenticated(
        sessionQuery.data.key_prefix ?? '',
        Boolean(sessionQuery.data.force_reset),
      )
      if (!getSavedSessionMarker()) localStorage.setItem('aigateway_session_active', '1')
    } else if (sessionQuery.data || sessionQuery.isError) {
      localStorage.removeItem('aigateway_session_active')
      clear()
    }
  }, [clear, sessionQuery.data, sessionQuery.isError, setAuthenticated])

  const login = async (username: string, password: string) => {
    const result = await loginWithPassword(username, password)
    const requiresReset = Boolean(result.force_reset)
    setAuthenticated(result.key_prefix, requiresReset)
    queryClient.setQueryData(queryKeys.auth.session, {
      authenticated: true,
      key_prefix: result.key_prefix,
      scopes: [],
      force_reset: requiresReset,
    })
    await queryClient.invalidateQueries({ queryKey: queryKeys.runtime.capabilities })
    return { forceReset: requiresReset }
  }

  const logout = async () => {
    try {
      await clearBrowserSession()
    } finally {
      clear()
      queryClient.removeQueries({ queryKey: queryKeys.auth.session })
      queryClient.removeQueries({ queryKey: queryKeys.runtime.capabilities })
    }
  }

  const completeForceReset = () => {
    setForceReset(false)
    queryClient.setQueryData(
      queryKeys.auth.session,
      (current: { force_reset?: boolean } | undefined) => (
        current ? { ...current, force_reset: false } : current
      ),
    )
  }

  return (
    <AuthContext.Provider value={{
      state: { apiKey: null, isAuthenticated, keyPrefix },
      isAuthenticated,
      keyPrefix,
      forceReset,
      // React effects synchronize the query result into Zustand after render.
      // Keep route guards in their loading state during that reconciliation
      // window so a valid browser session is not redirected to /login.
      isLoading: sessionQuery.isLoading || sessionStatePending,
      login,
      logout,
      completeForceReset,
    }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used within AuthProvider')
  return context
}
