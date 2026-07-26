import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthProvider, useAuth } from './AuthContext'
import { useAuthStore } from '@/stores/authStore'

const api = vi.hoisted(() => ({
  getBrowserSession: vi.fn(),
  getSavedApiKey: vi.fn(),
  saveApiKey: vi.fn(),
  clearApiKey: vi.fn(),
}))
vi.mock('@/api/client', () => api)

function Consumer() {
  const auth = useAuth()
  return (
    <div>
      <span>{auth.isAuthenticated ? `authenticated:${auth.keyPrefix}` : 'anonymous'}</span>
      <span>{auth.forceReset ? 'reset-required' : 'normal'}</span>
      <button onClick={() => void auth.login('gw-login-key')}>login</button>
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

describe('AuthProvider browser-session contract', () => {
  beforeEach(() => {
    localStorage.clear()
    useAuthStore.getState().clear()
    api.getBrowserSession.mockReset()
    api.getSavedApiKey.mockReset().mockReturnValue(null)
    api.saveApiKey.mockReset()
    api.clearApiKey.mockReset().mockResolvedValue(undefined)
  })

  it('hydrates an authenticated forced-reset session from the backend', async () => {
    api.getBrowserSession.mockResolvedValue({
      authenticated: true,
      key_prefix: 'gw-server',
      force_reset: true,
    })
    renderProvider()
    expect(await screen.findByText('authenticated:gw-server')).toBeInTheDocument()
    expect(screen.getByText('reset-required')).toBeInTheDocument()
    expect(localStorage.getItem('aigateway_session_active')).toBe('1')
  })

  it('logs in, completes reset and clears both store and query state on logout', async () => {
    api.getBrowserSession.mockResolvedValue({ authenticated: false })
    api.saveApiKey.mockResolvedValue({ key_prefix: 'gw-new', force_reset: true })
    const user = userEvent.setup()
    const { client } = renderProvider()
    await screen.findByText('anonymous')

    await user.click(screen.getByRole('button', { name: 'login' }))
    expect(await screen.findByText('authenticated:gw-new')).toBeInTheDocument()
    expect(api.saveApiKey).toHaveBeenCalledWith('gw-login-key')
    await user.click(screen.getByRole('button', { name: 'complete reset' }))
    expect(screen.getByText('normal')).toBeInTheDocument()
    expect(client.getQueryData(['auth', 'session'])).toMatchObject({ force_reset: false })

    await user.click(screen.getByRole('button', { name: 'logout' }))
    await waitFor(() => expect(screen.getByText('anonymous')).toBeInTheDocument())
    expect(api.clearApiKey).toHaveBeenCalled()
    expect(client.getQueryData(['auth', 'session'])).toEqual({ authenticated: false })
  })

  it('throws a useful error when consumed outside its provider', () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    expect(() => render(<Consumer />)).toThrow('useAuth must be used within AuthProvider')
    errorSpy.mockRestore()
  })
})
