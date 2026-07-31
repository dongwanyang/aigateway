import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach, vi } from 'vitest'

// The production config API always returns a revision for a complete config
// snapshot. Some broad page fixtures predate that contract and construct the
// same response with Response.json but omit the revision. Normalize only that
// exact full-config shape so integration tests exercise the current contract
// without weakening runtime validation in clientSafe.ts.
const responseJson = Response.json.bind(Response)
Object.defineProperty(Response, 'json', {
  configurable: true,
  writable: true,
  value: ((data: unknown, init?: ResponseInit) => {
    if (data && typeof data === 'object' && !Array.isArray(data)) {
      const envelope = data as {
        data?: unknown
        message?: unknown
        revision?: unknown
      }
      const payload = envelope.data
      if (
        envelope.message === 'success'
        && envelope.revision === undefined
        && payload
        && typeof payload === 'object'
        && !Array.isArray(payload)
        && 'server' in payload
        && 'providers' in payload
        && 'embedding' in payload
      ) {
        return responseJson({ ...envelope, revision: 'test-config-revision' }, init)
      }
    }
    return responseJson(data, init)
  }) as typeof Response.json,
})

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
