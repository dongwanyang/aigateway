import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import Layout from './Layout'
import { ThemeProvider, useTheme } from '@/hooks/useTheme'
import { initResizableTables } from '@/utils/resizableTable'
import { useAuthStore } from '@/stores/authStore'

const logout = vi.hoisted(() => vi.fn())
vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({
    state: { isAuthenticated: true, keyPrefix: 'gw-abcdef' },
    logout,
  }),
}))
vi.mock('@/components/CapabilityBanner', () => ({ default: () => <div>capability status</div> }))

function ThemeProbe() {
  const { theme, isDark, toggleTheme } = useTheme()
  return <button onClick={toggleTheme}>{theme}:{String(isDark)}</button>
}

describe('layout, theme and shared UI state', () => {
  beforeEach(() => {
    localStorage.clear()
    logout.mockReset().mockResolvedValue(undefined)
    useAuthStore.getState().clear()
  })

  it('navigates, toggles theme and logs out from the real layout controls', async () => {
    const user = userEvent.setup()
    render(
      <ThemeProvider>
        <MemoryRouter initialEntries={['/models']}>
          <Layout><div>page body</div></Layout>
        </MemoryRouter>
      </ThemeProvider>,
    )
    expect(screen.getByText('page body')).toBeInTheDocument()
    const activeModelLink = screen.getByRole('link', { name: /模型配置/ })
    expect(activeModelLink).toHaveAttribute('aria-current', 'page')
    expect(activeModelLink).toHaveClass('nav-item-active')
    expect(screen.getByText('gw-abcdef')).toBeInTheDocument()
    await user.click(screen.getByTitle('切换到亮色主题'))
    expect(document.documentElement).toHaveClass('light')
    expect(localStorage.getItem('aigateway_theme')).toBe('light')
    await user.click(screen.getByTitle('退出登录'))
    expect(logout).toHaveBeenCalled()
  })

  it('restores a saved theme and exposes context state', async () => {
    localStorage.setItem('aigateway_theme', 'light')
    const user = userEvent.setup()
    render(<ThemeProvider><ThemeProbe /></ThemeProvider>)
    expect(screen.getByRole('button', { name: 'light:false' })).toBeInTheDocument()
    await user.click(screen.getByRole('button'))
    expect(screen.getByRole('button', { name: 'dark:true' })).toBeInTheDocument()
  })

  it('resizes table columns only from the right-edge drag handle', () => {
    render(<table><thead><tr><th>Column</th></tr></thead></table>)
    const th = screen.getByText('Column')
    vi.spyOn(th, 'getBoundingClientRect').mockReturnValue({
      x: 0, y: 0, left: 0, top: 0, right: 100, bottom: 20,
      width: 100, height: 20, toJSON: () => ({}),
    })
    initResizableTables()
    fireEvent.mouseDown(th, { clientX: 95 })
    expect(document.body.style.cursor).toBe('col-resize')
    fireEvent.mouseMove(document, { clientX: 145 })
    expect(th).toHaveStyle({ width: '150px', minWidth: '150px' })
    fireEvent.mouseUp(document)
    expect(document.body.style.cursor).toBe('')
  })

  it('keeps authentication store transitions internally consistent', () => {
    useAuthStore.getState().setAuthenticated('gw-prod', true)
    expect(useAuthStore.getState()).toMatchObject({
      isAuthenticated: true,
      keyPrefix: 'gw-prod',
      forceReset: true,
    })
    useAuthStore.getState().setForceReset(false)
    expect(useAuthStore.getState().forceReset).toBe(false)
    useAuthStore.getState().clear()
    expect(useAuthStore.getState()).toMatchObject({
      isAuthenticated: false,
      keyPrefix: null,
      forceReset: false,
    })
  })
})
