import type { ApiError, ApiResponse } from '@/types'

// Keep the existing resource API behind one public module. Explicit exports
// below override the legacy config helpers from the internal implementation.
export * from './_clientCore'

const API_BASE = import.meta.env.VITE_API_BASE ?? ''
const FULL_CONFIG_REVISION = Symbol('fullConfigRevision')

type VersionedApiResponse<T> = ApiResponse<T> & { revision: string }
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
  if (typeof value !== 'string') return null
  let revision = value.trim()
  if (!revision || revision.startsWith('W/') || revision === '*' || revision.includes(',')) {
    return null
  }
  if (revision.startsWith('"') || revision.endsWith('"')) {
    if (!(revision.length >= 2 && revision[0] === '"' && revision.at(-1) === '"')) {
      return null
    }
    revision = revision.slice(1, -1).trim()
  }
  if (!revision || /["\r\n,]/.test(revision)) return null
  return revision
}

function strongRevisionFromEtag(value: string | null): string | null {
  if (!value) return null
  const etag = value.trim()
  if (etag.startsWith('W/') || etag === '*' || etag.includes(',')) return null
  const match = /^"([^"\r\n]+)"$/.exec(etag)
  return match ? normalizeRevision(match[1]) : null
}

function responseRevision(bodyRevision: unknown, response: Response): string | null {
  return normalizeRevision(bodyRevision) ?? strongRevisionFromEtag(response.headers.get('etag'))
}

function attachRevision(config: Record<string, unknown>, revision: string): void {
  // Enumerable symbols survive object spread, while JSON.stringify ignores
  // symbol keys. The revision therefore follows only the loaded snapshot and
  // is never persisted into config.yaml.
  Object.defineProperty(config, FULL_CONFIG_REVISION, {
    value: revision,
    writable: true,
    configurable: true,
    enumerable: true,
  })
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

  if (code === 'config_version_conflict') {
    message = '配置已被其他会话修改，请重新加载后再保存。'
  } else if (code === 'config_update_busy') {
    message = '另一个配置更新正在进行，请稍后重试。'
  } else if (
    code === 'config_revision_required'
    || code === 'config_precondition_required'
    || response.status === 428
  ) {
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

  const body = await response.json() as ApiResponse<Record<string, unknown>> & { revision?: unknown }
  if (!body.data || typeof body.data !== 'object' || Array.isArray(body.data)) {
    throw new ConfigApiError('配置响应格式无效。', 'invalid_config_response', 502)
  }
  const revision = responseRevision(body.revision, response)
  if (!revision) {
    throw new ConfigApiError(
      '配置响应缺少强 revision，请重新加载后再保存。',
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

  const body = await response.json() as ApiResponse<{ updated: boolean }> & { revision?: unknown }
  const nextRevision = responseRevision(body.revision, response)
  if (!nextRevision) {
    throw new ConfigApiError(
      '配置已保存，但响应缺少新的强 revision，请重新加载。',
      'config_revision_missing_after_update',
      502,
    )
  }
  attachRevision(config, nextRevision)
  return { ...body, revision: nextRevision }
}
