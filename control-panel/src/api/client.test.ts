import { afterEach, describe, expect, it, vi } from 'vitest'
import { rotateApiKey } from './client'

describe('rotateApiKey', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('sends the JSON request body required by the API', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({
        data: { key: 'gw-new-key', warning: 'save it' },
        message: 'success',
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    await rotateApiKey('key_123')

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringMatching(/\/admin\/api-keys\/key_123\/rotate$/),
      expect.objectContaining({
        method: 'POST',
        credentials: 'include',
        body: '{}',
      }),
    )
  })
})
