/**
 * Login page — exchanges an API key for an HttpOnly session cookie.
 *
 * If the user logs in with the default admin key, the backend returns
 * force_reset=true and the page switches to a password-reset flow.
 */
import { useEffect, useState, useRef } from 'react'
import { useNavigate } from 'react-router'
import { Shield, RefreshCw, AlertTriangle, Copy, CheckCircle } from 'lucide-react'
import Card from '@/components/Card'
import { useAuth } from '@/contexts/AuthContext'
import { resetPassword } from '@/api/client'

export default function Login() {
  const { login, isLoading, forceReset, logout, completeForceReset } = useAuth()
  const [apiKey, setApiKey] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const navigate = useNavigate()
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => { inputRef.current?.focus() }, [])

  async function handleLogin(e: React.FormEvent) {
    e.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      const result = await login(apiKey)
      if (!result.forceReset) navigate('/', { replace: true })
    } catch (error) {
      setError(error instanceof Error ? error.message : '登录失败，请检查 API Key')
    } finally {
      setSubmitting(false)
    }
  }

  // ── Force-reset flow ──────────────────────────────────────────────
  const [resetKey, setResetKey] = useState('')
  const [resetConfirm, setResetConfirm] = useState('')
  const [resetError, setResetError] = useState<string | null>(null)
  const [resetSuccess, setResetSuccess] = useState<{ key: string } | null>(null)
  const [resetSubmitting, setResetSubmitting] = useState(false)

  async function handleReset(e: React.FormEvent) {
    e.preventDefault()
    if (resetKey !== resetConfirm) {
      setResetError('两次输入的密钥不一致')
      return
    }
    if (resetKey.length < 20) {
      setResetError('新密钥长度不能少于 20 个字符')
      return
    }
    setResetSubmitting(true)
    setResetError(null)
    try {
      const resp = await resetPassword(resetKey)
      setResetSuccess({ key: resp.data.new_api_key })
      completeForceReset()
    } catch (error) {
      setResetError(error instanceof Error ? error.message : '重置失败')
    } finally {
      setResetSubmitting(false)
    }
  }

  function handleCopyKey(fullKey: string) {
    navigator.clipboard.writeText(fullKey).then(
      () => setTimeout(() => setResetSuccess({ key: fullKey }), 0),
      () => {}
    )
  }

  // ── Loading state ─────────────────────────────────────────────────
  if (isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center" style={{ backgroundColor: 'var(--color-bg-base)' }}>
        <RefreshCw size={32} className="animate-spin" style={{ color: 'var(--color-primary)' }} />
      </div>
    )
  }

  // ── Force-reset mode ──────────────────────────────────────────────
  if (forceReset) {
    if (resetSuccess) {
      return (
        <div className="min-h-screen flex items-center justify-center p-4" style={{ backgroundColor: 'var(--color-bg-base)' }}>
          <Card className="w-full max-w-md">
            <div className="text-center mb-6">
              <CheckCircle size={48} style={{ color: 'var(--color-success)' }} />
              <h1 className="text-xl font-bold mt-4" style={{ color: 'var(--color-success)' }}>
                密钥已重置
              </h1>
              <p className="text-sm mt-1" style={{ color: 'var(--color-text-tertiary)' }}>
                请立即复制保存以下新密钥，关闭后将无法再次查看
              </p>
            </div>
            <div style={{
              background: 'var(--color-bg-elevated)',
              border: '1px solid var(--color-warning)',
              borderRadius: '8px',
              padding: '16px',
              marginBottom: '16px',
            }}>
              <div className="flex items-center gap-2">
                <code style={{
                  fontFamily: 'var(--font-mono)',
                  fontSize: 'var(--font-size-md)',
                  wordBreak: 'break-all',
                  flex: 1,
                }}>{resetSuccess.key}</code>
                <button
                  className="btn btn-secondary"
                  style={{ padding: '8px 12px', minWidth: 'auto', whiteSpace: 'nowrap' }}
                  onClick={() => handleCopyKey(resetSuccess.key)}
                  title="复制到剪贴板"
                >
                  <Copy size={14} /> 复制
                </button>
              </div>
            </div>
            <button
              className="btn btn-primary w-full justify-center"
              onClick={() => navigate('/', { replace: true })}
            >
              进入控制台
            </button>
          </Card>
        </div>
      )
    }

    return (
      <div className="min-h-screen flex items-center justify-center p-4" style={{ backgroundColor: 'var(--color-bg-base)' }}>
        <Card className="w-full max-w-md">
          <div className="text-center mb-6">
            <AlertTriangle size={48} style={{ color: 'var(--color-warning)' }} />
            <h1 className="text-xl font-bold mt-4" style={{ color: 'var(--color-warning)' }}>
              检测到默认管理员密钥
            </h1>
            <p className="text-sm mt-1" style={{ color: 'var(--color-text-tertiary)' }}>
              为了安全起见，请立即重置管理员密钥
            </p>
          </div>
          <form onSubmit={handleReset} className="space-y-4">
            <div>
              <label className="block text-xs mb-1 font-medium" style={{ color: 'var(--color-text-tertiary)' }}>
                新 API Key
              </label>
              <input
                ref={inputRef}
                className="input w-full"
                type="password"
                placeholder="输入新的 API Key"
                value={resetKey}
                onChange={e => { setResetKey(e.target.value); setResetError(null) }}
                disabled={resetSubmitting}
              />
            </div>
            <div>
              <label className="block text-xs mb-1 font-medium" style={{ color: 'var(--color-text-tertiary)' }}>
                确认新 API Key
              </label>
              <input
                className="input w-full"
                type="password"
                placeholder="再次输入新 API Key"
                value={resetConfirm}
                onChange={e => { setResetConfirm(e.target.value); setResetError(null) }}
                disabled={resetSubmitting}
              />
            </div>
            {resetError && (
              <div className="text-sm" style={{ color: 'var(--color-danger)' }}>{resetError}</div>
            )}
            <button
              type="submit"
              className="btn btn-primary w-full justify-center"
              disabled={resetSubmitting || !resetKey.trim() || !resetConfirm.trim()}
            >
              {resetSubmitting ? '重置中...' : '重置密钥'}
            </button>
          </form>
          <div className="mt-4 text-center">
            <button
              className="text-xs"
              style={{ color: 'var(--color-text-tertiary)' }}
              onClick={() => { logout(); navigate('/login', { replace: true }) }}
            >
              取消并退出
            </button>
          </div>
        </Card>
      </div>
    )
  }

  // ── Normal login form ─────────────────────────────────────────────
  return (
    <div className="min-h-screen flex items-center justify-center p-4" style={{ backgroundColor: 'var(--color-bg-base)' }}>
      <Card className="w-full max-w-md">
        <div className="text-center mb-6">
          <Shield size={48} style={{ color: 'var(--color-primary)' }} />
          <h1 className="text-xl font-bold mt-4">AI Gateway Control Panel</h1>
          <p className="text-sm mt-1" style={{ color: 'var(--color-text-tertiary)' }}>
            使用 API Key 登录
          </p>
        </div>
        <form onSubmit={handleLogin} className="space-y-4">
          <div>
            <label className="block text-xs mb-1 font-medium" style={{ color: 'var(--color-text-tertiary)' }}>
              API Key
            </label>
            <input
              ref={inputRef}
              className="input w-full"
              type="password"
              placeholder="输入您的 API Key"
              value={apiKey}
              onChange={e => { setApiKey(e.target.value); setError(null) }}
              disabled={submitting}
            />
          </div>
          {error && (
            <div className="text-sm" style={{ color: 'var(--color-danger)' }}>{error}</div>
          )}
          <button
            type="submit"
            className="btn btn-primary w-full justify-center"
            disabled={submitting || !apiKey.trim()}
          >
            {submitting ? '登录中...' : '登录'}
          </button>
        </form>
      </Card>
    </div>
  )
}
