import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthProvider, useAuth } from './AuthContext'
import AuthGuard from '@/components/AuthGuard'
import { useAuthStore } from '@/stores/authStore'

const api = vi.hoisted(() => ({
  getBrowserSession: vi.fn(),
  getSavedSessionMarker: vi.fn(),
  loginWithPassword: vi.fn(),
  clearBrowserSession: vi.fn(),
}))
vi.mock('@/api/authSession', () => api)

function Consumer() {
  const auth = useAuth()
  return (
    <div>
      <span>{auth.isAuthenticated ? `authenticated:${auth.keyPrefix}` : 'anonymous'}</span>
      <span>{auth.forceReset ? 'reset-required' : 'normal'}</span>
      <button onClick={() => void auth.login('admin', 'admin-password')}>login</button>
      <button onClick={() => void auth.logout()}>logout</button>
      <button onClick={auth.completeForceReset}>complete reset</button>
    </div>
  )
}

function renderProvider() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return {
    client,
    ...render(<QueryClientProvider client={client}><AuthProvider><Consumer /></AuthProvider></QueryClientProvider>),
  }
}

function renderGuardedRoute() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/models']}>
        <AuthProvider>
          <Routes>
            <Route path="/login" element={<div>login page</div>} />
            <Route path="/models" element={<AuthGuard><div>models page</div></AuthGuard>} />
          </Routes>
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('AuthProvider browser-session contract', () => {
  beforeEach(() => {
    localStorage.clear()
    useAuthStore.getState().clear()
    api.getBrowserSession.mockReset()
    api.getSavedSessionMarker.mockReset().mockReturnValue(null)
    api.loginWithPassword.mockReset()
    api.clearBrowserSession.mockReset().mockResolvedValue(undefined)
  })

  it('hydrates an authenticated forced-reset session from the backend', async () => {
    api.getBrowserSession.mockResolvedValue({
      authenticated: true,
      key_prefix: 'admin',
      force_reset: true,
    })
    renderProvider()
    expect(await screen.findByText('authenticated:admin')).toBeInTheDocument()
    expect(screen.getByText('reset-required')).toBeInTheDocument()
    expect(localStorage.getItem('aigateway_session_active')).toBe('1')
  })

  it('keeps a refreshed protected route while restoring an authenticated session', async () => {
    api.getBrowserSession.mockResolvedValue({
      authenticated: true,
      key_prefix: 'admin',
      force_reset: false,
    })

    renderGuardedRoute()

    expect(await screen.findByText('models page')).toBeInTheDocument()
    expect(screen.queryByText('login page')).not.toBeInTheDocument()
  })

  it('logs in with username and password, completes reset, and clears state on logout', async () => {
    api.getBrowserSession.mockResolvedValue({ authenticated: false })
    api.loginWithPassword.mockResolvedValue({ key_prefix: 'admin', force_reset: true })
    const user = userEvent.setup()
    const { client } = renderProvider()
    await screen.findByText('anonymous')

    await user.click(screen.getByRole('button', { name: 'login' }))
    expect(await screen.findByText('authenticated:admin')).toBeInTheDocument()
    expect(api.loginWithPassword).toHaveBeenCalledWith('admin', 'admin-password')
    await user.click(screen.getByRole('button', { name: 'complete reset' }))
    expect(screen.getByText('normal')).toBeInTheDocument()
    expect(client.getQueryData(['auth', 'session'])).toMatchObject({ force_reset: false })

    await user.click(screen.getByRole('button', { name: 'logout' }))
    await waitFor(() => expect(screen.getByText('anonymous')).toBeInTheDocument())
    expect(api.clearBrowserSession).toHaveBeenCalled()
    expect(client.getQueryData(['auth', 'session'])).toEqual({ authenticated: false })
  })

  it('throws a useful error when consumed outside its provider', () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    expect(() => render(<Consumer />)).toThrow('useAuth must be used within AuthProvider')
    errorSpy.mockRestore()
  })
})
