import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import Layout from './Layout'

const auth = vi.hoisted(() => ({ logout: vi.fn() }))
const theme = vi.hoisted(() => ({ toggleTheme: vi.fn() }))

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({
    state: {
      isAuthenticated: true,
      keyPrefix: 'admin',
      apiKey: null,
    },
    logout: auth.logout,
  }),
}))

vi.mock('@/hooks/useTheme', () => ({
  useTheme: () => ({
    isDark: true,
    theme: 'dark',
    toggleTheme: theme.toggleTheme,
  }),
}))

vi.mock('@/components/CapabilityBanner', () => ({ default: () => null }))

function renderLayout(path = '/models') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Layout><div>页面内容</div></Layout>
    </MemoryRouter>,
  )
}

describe('refreshed control-panel layout', () => {
  beforeEach(() => {
    auth.logout.mockReset()
    theme.toggleTheme.mockReset()
  })

  it('shows grouped navigation and the current route context', () => {
    renderLayout('/models')

    expect(screen.getByText('页面内容')).toBeInTheDocument()
    expect(screen.getByText('工作台')).toBeInTheDocument()
    expect(screen.getByText('AI 能力')).toBeInTheDocument()
    expect(screen.getByText('运营治理')).toBeInTheDocument()
    expect(screen.getAllByText('模型配置')).toHaveLength(2)
    expect(screen.getAllByText('供应商与模型路由')).toHaveLength(2)
    expect(screen.getByText('admin')).toBeInTheDocument()
  })

  it('opens the mobile navigation drawer and closes it from the backdrop', async () => {
    const user = userEvent.setup()
    renderLayout('/')

    expect(screen.getAllByRole('link', { name: /概览/ })).toHaveLength(1)
    await user.click(screen.getByRole('button', { name: '打开导航' }))
    expect(screen.getAllByRole('link', { name: /概览/ })).toHaveLength(2)

    const closeButtons = screen.getAllByRole('button', { name: '关闭导航' })
    await user.click(closeButtons[0])
    expect(screen.getAllByRole('link', { name: /概览/ })).toHaveLength(1)
  })

  it('supports theme switching and logout', async () => {
    const user = userEvent.setup()
    renderLayout()

    await user.click(screen.getByRole('button', { name: '切换到亮色主题' }))
    expect(theme.toggleTheme).toHaveBeenCalledTimes(1)

    await user.click(screen.getByRole('button', { name: '退出登录' }))
    expect(auth.logout).toHaveBeenCalledTimes(1)
  })
})
