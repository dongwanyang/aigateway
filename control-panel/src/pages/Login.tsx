import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router'
import {
  AlertCircle,
  AlertTriangle,
  ArrowRight,
  Eye,
  EyeOff,
  Gauge,
  KeyRound,
  LockKeyhole,
  Moon,
  RefreshCw,
  Route,
  ShieldCheck,
  Sparkles,
  Sun,
  User,
} from 'lucide-react'
import { useAuth } from '@/contexts/AuthContext'
import { useTheme } from '@/hooks/useTheme'
import { getBootstrapCredentials, resetPassword } from '@/api/authSession'

function looksLikeGatewayApiKey(value: string): boolean {
  return value.trim().startsWith('gw-')
}

function AuthShowcase() {
  const features = [
    { icon: Route, title: '统一路由', copy: '集中管理模型、供应商与降级策略' },
    { icon: Gauge, title: '运营可见', copy: '实时查看延迟、成本、配额与缓存' },
    { icon: ShieldCheck, title: '安全边界', copy: '控制台会话与机器 API Key 完全分离' },
  ]

  return (
    <section className="auth-showcase" aria-label="AI Gateway 产品介绍">
      <div className="auth-brand">
        <div className="brand-mark" aria-hidden="true"><Sparkles size={19} /></div>
        <div>
          <div className="brand-name">AI Gateway</div>
          <div className="brand-subtitle">Control Plane</div>
        </div>
      </div>

      <div className="auth-hero">
        <div className="auth-kicker"><LockKeyhole size={13} /> Internal AI Infrastructure</div>
        <h1 className="auth-hero-title">
          管理每一次<br /><span>AI 请求流转</span>
        </h1>
        <p className="auth-hero-copy">
          通过一个清晰、安全的控制面统一管理模型路由、成本、配额、缓存与知识库，
          让 AI 基础设施保持可观测、可治理、可演进。
        </p>
        <div className="auth-feature-grid">
          {features.map(({ icon: Icon, title, copy }) => (
            <div className="auth-feature" key={title}>
              <div className="auth-feature-icon"><Icon size={16} /></div>
              <div className="auth-feature-title">{title}</div>
              <div className="auth-feature-copy">{copy}</div>
            </div>
          ))}
        </div>
      </div>

      <div className="auth-showcase-footer">
        <span className="status-dot" />
        管理端采用 HttpOnly Cookie Session，浏览器不保存 API Key
      </div>
    </section>
  )
}

function AuthPage({ children }: { children: ReactNode }) {
  const { toggleTheme, isDark } = useTheme()
  return (
    <div className="auth-shell">
      <AuthShowcase />
      <main className="auth-panel">
        <button
          type="button"
          className="icon-button auth-theme-button"
          onClick={toggleTheme}
          aria-label={isDark ? '切换到亮色主题' : '切换到暗色主题'}
          title={isDark ? '切换到亮色主题' : '切换到暗色主题'}
        >
          {isDark ? <Sun size={18} /> : <Moon size={18} />}
        </button>
        {children}
      </main>
    </div>
  )
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
      <div className="auth-loading" aria-label="正在加载控制台会话">
        <div className="auth-loading-mark">
          <RefreshCw size={23} className="animate-spin" />
        </div>
      </div>
    )
  }

  if (forceReset) {
    return (
      <AuthPage>
        <section className="auth-card">
          <header className="auth-card-header">
            <div className="auth-card-icon auth-card-icon-warning" aria-hidden="true">
              <AlertTriangle size={22} />
            </div>
            <h1 className="auth-card-title">设置独立管理员密码</h1>
            <p className="auth-card-copy">
              初始密码仅用于首次进入控制台。完成设置后，控制台登录密码与 API Key 将保持完全分离。
            </p>
          </header>

          <form onSubmit={handleReset} className="auth-form">
            <div>
              <label className="form-label" htmlFor="new-admin-password">新管理员密码</label>
              <div className="auth-input-wrap">
                <KeyRound size={17} className="auth-input-leading" />
                <input
                  id="new-admin-password"
                  ref={inputRef}
                  className="input has-leading-icon"
                  type="password"
                  autoComplete="new-password"
                  placeholder="至少 12 个字符"
                  value={newPassword}
                  onChange={event => { setNewPassword(event.target.value); setResetError(null) }}
                  disabled={resetSubmitting}
                />
              </div>
            </div>

            <div>
              <label className="form-label" htmlFor="confirm-admin-password">确认管理员密码</label>
              <div className="auth-input-wrap">
                <ShieldCheck size={17} className="auth-input-leading" />
                <input
                  id="confirm-admin-password"
                  className="input has-leading-icon"
                  type="password"
                  autoComplete="new-password"
                  placeholder="再次输入新密码"
                  value={confirmPassword}
                  onChange={event => { setConfirmPassword(event.target.value); setResetError(null) }}
                  disabled={resetSubmitting}
                />
              </div>
            </div>

            {resetError && (
              <div className="form-alert" role="alert">
                <AlertCircle size={16} className="mt-0.5 shrink-0" />
                <span>{resetError}</span>
              </div>
            )}

            <button type="submit" className="btn btn-primary auth-submit" disabled={resetSubmitting}>
              {resetSubmitting ? <><RefreshCw size={16} className="animate-spin" /> 设置中...</> : <>设置管理员密码 <ArrowRight size={16} /></>}
            </button>
          </form>

          <button
            type="button"
            className="auth-secondary-action"
            onClick={() => { void logout(); navigate('/login', { replace: true }) }}
          >
            取消并退出
          </button>
        </section>
      </AuthPage>
    )
  }

  return (
    <AuthPage>
      <section className="auth-card">
        <header className="auth-card-header">
          <div className="auth-card-icon" aria-hidden="true"><LockKeyhole size={22} /></div>
          <h1 className="auth-card-title">欢迎回来</h1>
          <p className="auth-card-copy">使用管理员账号和管理员密码进入 AI Gateway 控制台。</p>
        </header>

        <form onSubmit={handleLogin} className="auth-form">
          <div>
            <label className="form-label" htmlFor="admin-username">用户名</label>
            <div className="auth-input-wrap">
              <User size={17} className="auth-input-leading" />
              <input
                id="admin-username"
                ref={inputRef}
                className="input has-leading-icon"
                type="text"
                autoComplete="username"
                placeholder="管理员用户名"
                value={username}
                onChange={event => { credentialsTouched.current = true; setUsername(event.target.value); setError(null) }}
                disabled={submitting}
              />
            </div>
          </div>

          <div>
            <label className="form-label" htmlFor="admin-password">管理员密码</label>
            <div className="auth-input-wrap">
              <KeyRound size={17} className="auth-input-leading" />
              <input
                id="admin-password"
                className="input has-leading-icon has-trailing-action"
                type={showSecret ? 'text' : 'password'}
                autoComplete="current-password"
                placeholder="输入管理员密码"
                value={password}
                onChange={event => { credentialsTouched.current = true; setPassword(event.target.value); setError(null) }}
                disabled={submitting}
              />
              <button
                type="button"
                aria-label={showSecret ? '隐藏管理员密码' : '显示管理员密码'}
                className="auth-input-action"
                onClick={() => setShowSecret(value => !value)}
              >
                {showSecret ? <EyeOff size={17} /> : <Eye size={17} />}
              </button>
            </div>
            <p className="form-hint">
              API Key 仅用于 OpenAI 兼容的 /v1/* 程序化调用，不能用于控制台登录。
            </p>
          </div>

          {error && (
            <div className="form-alert" role="alert" aria-live="polite">
              <AlertCircle size={16} className="mt-0.5 shrink-0" />
              <span>{error}</span>
            </div>
          )}

          <button
            type="submit"
            className="btn btn-primary auth-submit"
            disabled={submitting || !username.trim() || !password}
          >
            {submitting ? <><RefreshCw size={16} className="animate-spin" /> 登录中...</> : <>登录 <ArrowRight size={16} /></>}
          </button>
        </form>

        <div className="auth-security-note">
          <ShieldCheck size={15} className="mt-0.5 shrink-0" />
          登录后由服务端签发不可读的 HttpOnly Session Cookie；浏览器不会保存管理员密码或机器 API Key。
        </div>
      </section>
    </AuthPage>
  )
}
