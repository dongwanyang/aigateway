/**
 * Route guard — redirects unauthenticated users to /login.
 */
import { Navigate, useLocation } from 'react-router'
import { RefreshCw } from 'lucide-react'
import { useAuth } from '@/contexts/AuthContext'

export default function AuthGuard({ children }: { children: React.ReactNode }) {
  const { state, forceReset, isLoading } = useAuth()
  const location = useLocation()

  if (isLoading) {
    return (
      <div className="flex items-center justify-center min-h-[60vh]">
        <RefreshCw size={24} className="animate-spin" style={{ color: 'var(--color-primary)' }} />
      </div>
    )
  }

  if (!state.isAuthenticated) {
    return <Navigate to="/login" replace state={{ from: location }} />
  }

  if (forceReset) {
    return <Navigate to="/login" replace />
  }

  return <>{children}</>
}
