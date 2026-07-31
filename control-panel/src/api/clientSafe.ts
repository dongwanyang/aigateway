import type { ApiError, ApiResponse } from '@/types'

export * from './client'

const API_BASE = import.meta.env.VITE_API_BASE ?? ''
const FULL_CONFIG_REVISION = Symbol('fullConfigRevision')

type VersionedApiResponse<T> = ApiResponse<T> & { revision?: string }
type RevisionTaggedConfig = Record<string, unknown> & {
  [FULL_CONFIG_REVISION]?: string
}
type ApiErrorEnvelope = Partial<ApiError> & {
  detail?: { error?: { code?: string; message?: string } } | string
}

class ConfigApiError extends Error {
  readonly code: string
  readonly status: number

  constructor(message: string, code: string, status: number) {
    super(message)
    this.name = 'ConfigApiError'
    this.code = code
    this.status = status
  }
}

function normalizeRevision(value: unknown): string | null {
  if (typeof value !== 'string' || !value.trim()) return null
  return value.trim().replace(/^W\//, '').replace(/^"|"$/g, '') || null
}

function attachRevision(config: Record<string, unknown>, revision: string): void {
  // Symbol properties survive object spread but are ignored by JSON.stringify.
  // This keeps the revision bound to the exact configuration snapshot without
  // leaking it into config.yaml or sharing it across browser sessions.
  ;(config as RevisionTaggedConfig)[FULL_CONFIG_REVISION] = revision
}

function getAttachedRevision(config: Record<string, unknown>): string | null {
  return normalizeRevision((config as RevisionTaggedConfig)[FULL_CONFIG_REVISION])
}

async function configError(response: Response): Promise<ConfigApiError> {
  let code = 'unknown_error'
  let message = `HTTP ${response.status}`

  try {
    const body = await response.json() as ApiErrorEnvelope
    const nested = typeof body.detail === 'object' ? body.detail?.error : undefined
    code = body.error?.code ?? nested?.code ?? code
    message = body.error?.message
      ?? nested?.message
      ?? (typeof body.detail === 'string' ? body.detail : message)
  } catch {
    message = `Server error: ${response.status} ${response.statusText}`
  }

  if (code === 'config_version_conflict' || response.status === 409) {
    message = '配置已被其他会话修改，请重新加载后再保存。'
  } else if (code === 'config_precondition_required' || response.status === 428) {
    message = '配置尚未成功加载，请重新加载后再保存。'
  }

  return new ConfigApiError(message, code, response.status)
}

export async function getFullConfig(): Promise<VersionedApiResponse<Record<string, unknown>>> {
  const response = await fetch(`${API_BASE}/admin/config`, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
  })
  if (!response.ok) throw await configError(response)

  const body = await response.json() as VersionedApiResponse<Record<string, unknown>>
  const revision = normalizeRevision(body.revision ?? response.headers.get('etag'))
  if (!revision) {
    throw new ConfigApiError(
      '配置响应缺少 revision，请重新加载后再保存。',
      'config_revision_missing',
      502,
    )
  }

  attachRevision(body.data, revision)
  return { ...body, revision }
}

export async function updateFullConfig(
  config: Record<string, unknown>,
): Promise<VersionedApiResponse<{ updated: boolean }>> {
  const revision = getAttachedRevision(config)
  if (!revision) {
    throw new ConfigApiError(
      '配置尚未成功加载，请重新加载后再保存。',
      'config_precondition_required',
      428,
    )
  }

  const response = await fetch(`${API_BASE}/admin/config`, {
    method: 'PUT',
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      'If-Match': `"${revision}"`,
    },
    body: JSON.stringify(config),
  })
  if (!response.ok) throw await configError(response)

  const body = await response.json() as VersionedApiResponse<{ updated: boolean }>
  const nextRevision = normalizeRevision(body.revision ?? response.headers.get('etag')) ?? revision
  attachRevision(config, nextRevision)
  return { ...body, revision: nextRevision }
}
