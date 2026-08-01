import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach, vi } from 'vitest'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}

globalThis.ResizeObserver = ResizeObserverMock as typeof ResizeObserver
HTMLElement.prototype.scrollIntoView = vi.fn()

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
})

// The broad page-integration fixture predates strong config revisions and
// builds responses through Response.json. Keep that fixture aligned with the
// production admin-config contract without weakening the API client itself.
const nativeResponseJson = Response.json.bind(Response)

function responseWithRevision(
  payload: Record<string, unknown>,
  revision: string,
  init?: ResponseInit,
): Response {
  const headers = new Headers(init?.headers)
  headers.set('ETag', `"${revision}"`)
  return nativeResponseJson(
    { ...payload, revision },
    { ...init, headers },
  )
}

Response.json = ((data: unknown, init?: ResponseInit): Response => {
  if (!data || typeof data !== 'object' || Array.isArray(data)) {
    return nativeResponseJson(data, init)
  }

  const envelope = data as Record<string, unknown>
  if ('revision' in envelope) return nativeResponseJson(data, init)

  const body = envelope.data
  if (body && typeof body === 'object' && !Array.isArray(body)) {
    const record = body as Record<string, unknown>
    const isLegacyFullConfigFixture = (
      'server' in record
      && 'providers' in record
      && 'embedding' in record
    )
    if (isLegacyFullConfigFixture) {
      return responseWithRevision(envelope, 'integration-revision-1', init)
    }
    if (record.updated === true) {
      return responseWithRevision(envelope, 'integration-revision-2', init)
    }
  }

  return nativeResponseJson(data, init)
}) as typeof Response.json
