const API_BASE = import.meta.env.VITE_API_BASE ?? ''
const SESSION_MARKER = 'aigateway_session_active'

if (typeof window !== 'undefined') {
  window.localStorage.removeItem('aigateway_api_key')
}

export interface LoginResult {
  key_prefix: string
  force_reset?: boolean
}

export interface BootstrapCredentials {
  available: boolean
  username?: string
  initial_password?: string
}

export interface BrowserSession {
  authenticated: boolean
  key_prefix?: string
  scopes?: string[]
  force_reset?: boolean
}

export async function loginWithPassword(
  username: string,
  password: string,
): Promise<LoginResult> {
  const res = await fetch(`${API_BASE}/auth/session`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  if (!res.ok) {
    let message = 'Invalid username or password'
    try {
      const body = await res.json()
      message = body.error?.message ?? body.detail?.error?.message ?? message
    } catch {
      // Non-JSON error response (e.g., HTML nginx page)
    }
    const err = new Error(message)
    ;(err as any).status = res.status
    throw err
  }
  localStorage.removeItem('aigateway_api_key')
  localStorage.setItem(SESSION_MARKER, '1')
  const data = await res.json()
  return {
    key_prefix: data.data?.key_prefix ?? username,
    force_reset: data.data?.force_reset,
  }
}

export async function getBootstrapCredentials(): Promise<BootstrapCredentials> {
  const res = await fetch(`${API_BASE}/auth/bootstrap`, {
    credentials: 'include',
    cache: 'no-store',
  })
  if (!res.ok) return { available: false }
  const body = await res.json()
  return body.data ?? { available: false }
}

export async function clearBrowserSession(): Promise<void> {
  await fetch(`${API_BASE}/auth/session`, {
    method: 'DELETE',
    credentials: 'include',
  })
  localStorage.removeItem('aigateway_api_key')
  localStorage.removeItem(SESSION_MARKER)
}

export function getSavedSessionMarker(): string | null {
  return localStorage.getItem(SESSION_MARKER)
}

async function fetchSessionJson<T>(path: string): Promise<{ data: T; message?: string }> {
  const res = await fetch(`${API_BASE}${path}`, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
  })
  if (!res.ok) {
    let message = `HTTP ${res.status}`
    try {
      const body = await res.json()
      message = body.error?.message ?? message
    } catch {
      message = `Server error: ${res.status} ${res.statusText}`
    }
    const err = new Error(message)
    ;(err as any).status = res.status
    throw err
  }
  return res.json()
}

export async function getBrowserSession(): Promise<BrowserSession> {
  return (await fetchSessionJson<BrowserSession>('/auth/session')).data
}

export async function resetPassword(
  newPassword: string,
): Promise<{ data: { password_changed: boolean; warning: string } }> {
  const res = await fetch(`${API_BASE}/auth/reset-password`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ new_password: newPassword }),
  })
  if (!res.ok) {
    let message = '密码设置失败'
    try {
      const body = await res.json()
      message = body.error?.message ?? body.detail?.error?.message ?? body.detail ?? message
    } catch {}
    const err = new Error(message)
    ;(err as any).status = res.status
    throw err
  }
  return res.json()
}
