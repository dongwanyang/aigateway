import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router'
import { AlertTriangle, Eye, EyeOff, RefreshCw, Shield, User } from 'lucide-react'
import Card from '@/components/Card'
import { useAuth } from '@/contexts/AuthContext'
import { getBootstrapCredentials, resetPassword } from '@/api/authSession'

function looksLikeGatewayApiKey(value: string): boolean {
  return value.trim().startsWith('gw-')
}

export default function Login() {
  const { login, isAuthenticated, isLoading, forceReset, logout, completeForceReset } = useAuth()
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')
  const [showSecret, setShowSecret] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [resetError, setResetError] = useState<string | null>(null)
  const [resetSubmitting, setResetSubmitting] = useState(false)
  const credentialsTouched = useRef(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const navigate = useNavigate()

  useEffect(() => { inputRef.current?.focus() }, [])

  useEffect(() => {
    if (!isLoading && isAuthenticated && !forceReset) {
      navigate('/', { replace: true })
    }
  }, [forceReset, isAuthenticated, isLoading, navigate])

  useEffect(() => {
    if (isLoading || isAuthenticated || forceReset) return
    let cancelled = false
    getBootstrapCredentials()
      .then(credentials => {
        if (cancelled || credentialsTouched.current || !credentials.available) return
        setUsername(credentials.username ?? 'admin')
        setPassword(credentials.initial_password ?? '')
      })
      .catch(() => {})
    return () => { cancelled = true }
  }, [forceReset, isAuthenticated, isLoading])

  async function handleLogin(event: React.FormEvent) {
    event.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      if (looksLikeGatewayApiKey(password)) {
        throw new Error('API Key 不能用于控制台登录。请使用初始管理员密码，或首次登录后设置的管理员密码。')
      }
      const result = await login(username, password)
      if (!result.forceReset) navigate('/', { replace: true })
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '登录失败，请检查用户名和管理员密码')
    } finally {
      setSubmitting(false)
    }
  }

  async function handleReset(event: React.FormEvent) {
    event.preventDefault()
    setResetError(null)
    if (newPassword.length < 12) {
      setResetError('管理员密码至少需要 12 个字符')
      return
    }
    if (newPassword !== confirmPassword) {
      setResetError('两次输入的密码不一致')
      return
    }
    setResetSubmitting(true)
    try {
      await resetPassword(newPassword)
      completeForceReset()
      navigate('/', { replace: true })
    } catch (reason) {
      setResetError(reason instanceof Error ? reason.message : '管理员密码设置失败')
    } finally {
      setResetSubmitting(false)
    }
  }

  if (isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center" style={{ backgroundColor: 'var(--color-bg-base)' }}>
        <RefreshCw size={32} className="animate-spin" style={{ color: 'var(--color-primary)' }} />
      </div>
    )
  }

  if (forceReset) {
    return (
      <div className="min-h-screen flex items-center justify-center p-4" style={{ backgroundColor: 'var(--color-bg-base)' }}>
        <Card className="w-full max-w-md">
          <div className="text-center mb-6">
            <AlertTriangle size={48} style={{ color: 'var(--color-warning)', margin: '0 auto' }} />
            <h1 className="text-xl font-bold mt-4">设置独立管理员密码</h1>
            <p className="text-sm mt-1" style={{ color: 'var(--color-text-tertiary)' }}>
              初始密码仅用于首次进入控制台。设置后，控制台登录密码与 API Key 完全分离。
            </p>
          </div>
          <form onSubmit={handleReset} className="space-y-4">
            <div>
              <label className="block text-xs mb-1 font-medium" style={{ color: 'var(--color-text-tertiary)' }}>新管理员密码</label>
              <input
                ref={inputRef}
                className="input w-full"
                type="password"
                autoComplete="new-password"
                value={newPassword}
                onChange={event => { setNewPassword(event.target.value); setResetError(null) }}
                disabled={resetSubmitting}
              />
            </div>
            <div>
              <label className="block text-xs mb-1 font-medium" style={{ color: 'var(--color-text-tertiary)' }}>确认管理员密码</label>
              <input
                className="input w-full"
                type="password"
                autoComplete="new-password"
                value={confirmPassword}
                onChange={event => { setConfirmPassword(event.target.value); setResetError(null) }}
                disabled={resetSubmitting}
              />
            </div>
            {resetError && <div className="text-sm" style={{ color: 'var(--color-danger)' }}>{resetError}</div>}
            <button type="submit" className="btn btn-primary w-full justify-center" disabled={resetSubmitting}>
              {resetSubmitting ? '设置中...' : '设置管理员密码'}
            </button>
          </form>
          <button
            type="button"
            className="w-full mt-4 text-xs"
            style={{ color: 'var(--color-text-tertiary)' }}
            onClick={() => { void logout(); navigate('/login', { replace: true }) }}
          >
            取消并退出
          </button>
        </Card>
      </div>
    )
  }

  return (
    <div className="min-h-screen flex items-center justify-center p-4" style={{ backgroundColor: 'var(--color-bg-base)' }}>
      <Card className="w-full max-w-md">
        <div className="text-center mb-6">
          <Shield size={48} style={{ color: 'var(--color-primary)', margin: '0 auto' }} />
          <h1 className="text-xl font-bold mt-4">AI Gateway Control Panel</h1>
          <p className="text-sm mt-1" style={{ color: 'var(--color-text-tertiary)' }}>使用管理员账号和管理员密码登录</p>
        </div>
        <form onSubmit={handleLogin} className="space-y-4">
          <div>
            <label className="block text-xs mb-1 font-medium" style={{ color: 'var(--color-text-tertiary)' }}>用户名</label>
            <div className="relative">
              <User size={16} className="absolute left-3 top-1/2 -translate-y-1/2" style={{ color: 'var(--color-text-tertiary)' }} />
              <input
                ref={inputRef}
                className="input w-full"
                style={{ paddingLeft: '38px' }}
                type="text"
                autoComplete="username"
                value={username}
                onChange={event => { credentialsTouched.current = true; setUsername(event.target.value); setError(null) }}
                disabled={submitting}
              />
            </div>
          </div>
          <div>
            <label className="block text-xs mb-1 font-medium" style={{ color: 'var(--color-text-tertiary)' }}>管理员密码</label>
            <div className="relative">
              <input
                className="input w-full"
                style={{ paddingRight: '42px' }}
                type={showSecret ? 'text' : 'password'}
                autoComplete="current-password"
                value={password}
                onChange={event => { credentialsTouched.current = true; setPassword(event.target.value); setError(null) }}
                disabled={submitting}
              />
              <button
                type="button"
                aria-label={showSecret ? '隐藏管理员密码' : '显示管理员密码'}
                className="absolute right-3 top-1/2 -translate-y-1/2"
                style={{ color: 'var(--color-text-tertiary)' }}
                onClick={() => setShowSecret(value => !value)}
              >
                {showSecret ? <EyeOff size={17} /> : <Eye size={17} />}
              </button>
            </div>
            <p className="text-xs mt-1" style={{ color: 'var(--color-text-tertiary)' }}>
              API Key 仅用于 OpenAI 兼容的 /v1/* 程序化调用，不能用于控制台登录。
            </p>
          </div>
          {error && <div className="text-sm" style={{ color: 'var(--color-danger)' }}>{error}</div>}
          <button type="submit" className="btn btn-primary w-full justify-center" disabled={submitting || !username.trim() || !password}>
            {submitting ? '登录中...' : '登录'}
          </button>
        </form>
      </Card>
    </div>
  )
}
