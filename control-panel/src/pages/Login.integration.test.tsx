import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import Login from './Login'

const auth = vi.hoisted(() => ({
  login: vi.fn(),
  logout: vi.fn(),
  completeForceReset: vi.fn(),
  isAuthenticated: false,
  isLoading: false,
  forceReset: false,
  state: { isAuthenticated: false },
}))
const resetPassword = vi.hoisted(() => vi.fn())
const getBootstrapCredentials = vi.hoisted(() => vi.fn())

vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => auth }))
vi.mock('@/api/authSession', () => ({
  resetPassword,
  getBootstrapCredentials,
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

function currentPasswordInput(container: HTMLElement): HTMLInputElement {
  const input = container.querySelector('input[autocomplete="current-password"]')
  if (!(input instanceof HTMLInputElement)) throw new Error('current password input not found')
  return input
}

function usernameInput(container: HTMLElement): HTMLInputElement {
  const input = container.querySelector('input[autocomplete="username"]')
  if (!(input instanceof HTMLInputElement)) throw new Error('username input not found')
  return input
}

function resetPasswordInputs(container: HTMLElement): HTMLInputElement[] {
  const inputs = Array.from(container.querySelectorAll('input[autocomplete="new-password"]'))
  if (inputs.length !== 2 || !inputs.every(input => input instanceof HTMLInputElement)) {
    throw new Error('reset password inputs not found')
  }
  return inputs as HTMLInputElement[]
}

describe('Login authentication and forced password reset', () => {
  beforeEach(() => {
    auth.login.mockReset()
    auth.logout.mockReset()
    auth.completeForceReset.mockReset()
    resetPassword.mockReset()
    getBootstrapCredentials.mockReset().mockResolvedValue({ available: false })
    auth.isLoading = false
    auth.forceReset = false
    auth.isAuthenticated = false
  })

  it('leaves the login page when an existing browser session is restored', async () => {
    auth.isAuthenticated = true
    renderLogin()

    expect(await screen.findByText('控制台首页')).toBeInTheDocument()
  })

  it('submits username and password and navigates after a normal login', async () => {
    auth.login.mockResolvedValue({ forceReset: false })
    const user = userEvent.setup()
    const { container } = renderLogin()

    await user.clear(usernameInput(container))
    await user.type(usernameInput(container), 'admin')
    await user.type(currentPasswordInput(container), 'admin-password')
    await user.click(screen.getByRole('button', { name: '登录' }))

    await waitFor(() => expect(auth.login).toHaveBeenCalledWith('admin', 'admin-password'))
    expect(await screen.findByText('控制台首页')).toBeInTheDocument()
  })

  it('shows the backend login error and keeps the user on the form', async () => {
    auth.login.mockRejectedValue(new Error('用户名或密码无效'))
    const user = userEvent.setup()
    const { container } = renderLogin()

    await user.type(currentPasswordInput(container), 'bad-password')
    await user.click(screen.getByRole('button', { name: '登录' }))

    expect(await screen.findByText('用户名或密码无效')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '登录' })).toBeEnabled()
  })

  it('rejects gateway API keys before calling the console login API', async () => {
    const user = userEvent.setup()
    const { container } = renderLogin()

    await user.type(currentPasswordInput(container), 'gw-1234567890abcdef')
    await user.click(screen.getByRole('button', { name: '登录' }))

    expect(await screen.findByText('API Key 不能用于控制台登录。请使用初始管理员密码，或首次登录后设置的管理员密码。')).toBeInTheDocument()
    expect(auth.login).not.toHaveBeenCalled()
  })

  it('prefills generated bootstrap credentials and supports account login', async () => {
    getBootstrapCredentials.mockResolvedValue({
      available: true,
      username: 'admin',
      initial_password: 'temporary-admin-password',
    })
    auth.login.mockResolvedValue({ forceReset: true })
    const user = userEvent.setup()
    renderLogin()

    expect(await screen.findByDisplayValue('temporary-admin-password')).toBeInTheDocument()
    expect(screen.getByDisplayValue('admin')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '登录' }))
    expect(auth.login).toHaveBeenCalledWith('admin', 'temporary-admin-password')
  })

  it('validates both forced-reset fields before calling the API', async () => {
    auth.forceReset = true
    const user = userEvent.setup()
    const { container } = renderLogin()
    const [password, confirmation] = resetPasswordInputs(container)

    await user.type(password, 'abcdefghijkl')
    await user.type(confirmation, 'different-password')
    await user.click(screen.getByRole('button', { name: '设置管理员密码' }))
    expect(screen.getByText('两次输入的密码不一致')).toBeInTheDocument()

    await user.clear(password)
    await user.clear(confirmation)
    await user.type(password, 'short')
    await user.type(confirmation, 'short')
    await user.click(screen.getByRole('button', { name: '设置管理员密码' }))
    expect(screen.getByText('管理员密码至少需要 12 个字符')).toBeInTheDocument()
    expect(resetPassword).not.toHaveBeenCalled()
  })

  it('sets the new administrator password and enters the console', async () => {
    auth.forceReset = true
    resetPassword.mockResolvedValue({ data: { password_changed: true } })
    const user = userEvent.setup()
    const { container } = renderLogin()
    const [password, confirmation] = resetPasswordInputs(container)

    await user.type(password, 'abcdefghijkl')
    await user.type(confirmation, 'abcdefghijkl')
    await user.click(screen.getByRole('button', { name: '设置管理员密码' }))

    await waitFor(() => expect(resetPassword).toHaveBeenCalledWith('abcdefghijkl'))
    expect(auth.completeForceReset).toHaveBeenCalled()
    expect(await screen.findByText('控制台首页')).toBeInTheDocument()
  })

  it('logs out when a forced reset is cancelled', async () => {
    auth.forceReset = true
    const user = userEvent.setup()
    renderLogin()

    await user.click(screen.getByRole('button', { name: '取消并退出' }))
    expect(auth.logout).toHaveBeenCalled()
  })
})
