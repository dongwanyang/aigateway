import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import Login from './Login'

const auth = vi.hoisted(() => ({
  login: vi.fn(),
  logout: vi.fn(),
  completeForceReset: vi.fn(),
  isLoading: false,
  forceReset: false,
  state: { isAuthenticated: false },
}))
const resetPassword = vi.hoisted(() => vi.fn())

vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => auth }))
vi.mock('@/api/client', async importOriginal => ({
  ...await importOriginal<typeof import('@/api/client')>(),
  resetPassword,
}))

function renderLogin() {
  return render(
    <MemoryRouter initialEntries={['/login']}>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/" element={<div>控制台首页</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('Login authentication and forced key reset', () => {
  beforeEach(() => {
    auth.login.mockReset()
    auth.logout.mockReset()
    auth.completeForceReset.mockReset()
    resetPassword.mockReset()
    auth.isLoading = false
    auth.forceReset = false
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
    })
  })

  it('submits the entered key and navigates only after a normal login', async () => {
    auth.login.mockResolvedValue({ forceReset: false })
    const user = userEvent.setup()
    renderLogin()

    await user.type(screen.getByPlaceholderText('输入您的 API Key'), 'gw-live-key')
    await user.click(screen.getByRole('button', { name: '登录' }))

    await waitFor(() => expect(auth.login).toHaveBeenCalledWith('gw-live-key'))
    expect(await screen.findByText('控制台首页')).toBeInTheDocument()
  })

  it('shows the backend login error and keeps the user on the form', async () => {
    auth.login.mockRejectedValue(new Error('API Key 无效'))
    const user = userEvent.setup()
    renderLogin()

    await user.type(screen.getByPlaceholderText('输入您的 API Key'), 'bad-key')
    await user.click(screen.getByRole('button', { name: '登录' }))

    expect(await screen.findByText('API Key 无效')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '登录' })).toBeEnabled()
  })

  it('validates both forced-reset fields before calling the API', async () => {
    auth.forceReset = true
    const user = userEvent.setup()
    renderLogin()
    const key = screen.getByPlaceholderText('输入新的 API Key')
    const confirmation = screen.getByPlaceholderText('再次输入新 API Key')

    await user.type(key, 'abcdefghijklmnopqrst')
    await user.type(confirmation, 'different-key-value-123')
    await user.click(screen.getByRole('button', { name: '重置密钥' }))
    expect(screen.getByText('两次输入的密钥不一致')).toBeInTheDocument()

    await user.clear(key)
    await user.clear(confirmation)
    await user.type(key, 'short')
    await user.type(confirmation, 'short')
    await user.click(screen.getByRole('button', { name: '重置密钥' }))
    expect(screen.getByText('新密钥长度不能少于 20 个字符')).toBeInTheDocument()
    expect(resetPassword).not.toHaveBeenCalled()
  })

  it('resets, exposes the one-time key, copies it and enters the console', async () => {
    auth.forceReset = true
    resetPassword.mockResolvedValue({ data: { new_api_key: 'gw-new-abcdefghijklmnop' } })
    const user = userEvent.setup()
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    })
    renderLogin()

    await user.type(screen.getByPlaceholderText('输入新的 API Key'), 'abcdefghijklmnopqrst')
    await user.type(screen.getByPlaceholderText('再次输入新 API Key'), 'abcdefghijklmnopqrst')
    await user.click(screen.getByRole('button', { name: '重置密钥' }))

    expect(await screen.findByText('gw-new-abcdefghijklmnop')).toBeInTheDocument()
    expect(resetPassword).toHaveBeenCalledWith('abcdefghijklmnopqrst')
    expect(auth.completeForceReset).toHaveBeenCalled()
    await user.click(screen.getByTitle('复制到剪贴板'))
    expect(writeText).toHaveBeenCalledWith('gw-new-abcdefghijklmnop')
    await user.click(screen.getByRole('button', { name: '进入控制台' }))
    expect(await screen.findByText('控制台首页')).toBeInTheDocument()
  })

  it('logs out when a forced reset is cancelled', async () => {
    auth.forceReset = true
    const user = userEvent.setup()
    renderLogin()
    await user.click(screen.getByRole('button', { name: '取消并退出' }))
    expect(auth.logout).toHaveBeenCalled()
    expect(screen.getByText('检测到默认管理员密钥')).toBeInTheDocument()
  })
})
