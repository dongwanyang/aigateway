import { afterEach, describe, expect, it, vi } from 'vitest'
import * as api from './client'
import * as auth from './authSession'

const jsonResponse = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })

afterEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('browser authentication and streaming contracts', () => {
  it('logs in with username/password and clears an HttpOnly browser-session marker', async () => {
    localStorage.setItem('aigateway_api_key', 'legacy-secret')
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse({
        data: { key_prefix: 'admin', force_reset: true },
      }))
      .mockResolvedValueOnce(jsonResponse({}))

    const session = await auth.loginWithPassword('admin', 'admin-password')
    expect(session).toEqual({ key_prefix: 'admin', force_reset: true })
    expect(localStorage.getItem('aigateway_api_key')).toBeNull()
    expect(auth.getSavedSessionMarker()).toBe('1')
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      expect.stringMatching(/\/auth\/session$/),
      expect.objectContaining({
        method: 'POST',
        credentials: 'include',
        body: JSON.stringify({ username: 'admin', password: 'admin-password' }),
      }),
    )

    await auth.clearBrowserSession()
    expect(auth.getSavedSessionMarker()).toBeNull()
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      expect.stringMatching(/\/auth\/session$/),
      { method: 'DELETE', credentials: 'include' },
    )
  })

  it('returns password-login server errors instead of a false login success', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ error: { message: 'invalid password' } }, 401),
    )
    await expect(auth.loginWithPassword('admin', 'wrong')).rejects.toMatchObject({
      message: 'invalid password',
      status: 401,
    })
  })

  it('supports no-store bootstrap credentials and password reset payloads', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse({
        data: {
          available: true,
          username: 'admin',
          initial_password: 'temporary-password',
        },
      }))
      .mockResolvedValueOnce(jsonResponse({
        data: { password_changed: true, warning: 'changed' },
      }))

    await expect(auth.getBootstrapCredentials()).resolves.toMatchObject({
      available: true,
      username: 'admin',
    })
    await expect(auth.resetPassword('new-admin-password')).resolves.toEqual({
      data: { password_changed: true, warning: 'changed' },
    })
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      expect.stringMatching(/\/auth\/bootstrap$/),
      expect.objectContaining({ cache: 'no-store' }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      expect.stringMatching(/\/auth\/reset-password$/),
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ new_password: 'new-admin-password' }),
      }),
    )
  })

  it('routes control-panel chat through the admin console endpoint', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(jsonResponse({
      data: { choices: [] },
      message: 'success',
    }))

    await api.createChatCompletion({ model: 'auto', messages: [] } as any)

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringMatching(/\/admin\/console\/chat\/completions$/),
      expect.objectContaining({
        method: 'POST',
        credentials: 'include',
        body: JSON.stringify({ model: 'auto', messages: [] }),
      }),
    )
  })

  it('distinguishes draft JSON from an SSE stream and forwards cancellation', async () => {
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('data: ok\n\n'))
        controller.close()
      },
    })
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse({
        data: {
          draft_id: 'draft/1',
          preview_url: '/preview',
          generation_params: { media_type: 'video', seed: 7 },
        },
        _meta: { draft_pending_confirmation: true },
      }))
      .mockResolvedValueOnce(new Response(stream, {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
      }))
    const controller = new AbortController()

    await expect(api.requestChatCompletion({
      model: 'auto',
      messages: [{ role: 'user', content: 'make video' }],
    } as any, controller.signal)).resolves.toEqual({
      kind: 'draft',
      draftId: 'draft/1',
      previewUrl: '/preview',
      mediaType: 'video',
      generationParams: { media_type: 'video', seed: 7 },
    })
    const streamed = await api.requestChatCompletion({
      model: 'auto',
      messages: [{ role: 'user', content: 'hello' }],
    } as any, controller.signal)
    expect(streamed.kind).toBe('stream')
    if (streamed.kind === 'stream') expect(streamed.body).toBe(stream)
    expect(fetchMock.mock.calls[0][0]).toEqual(
      expect.stringMatching(/\/admin\/console\/chat\/completions$/),
    )
    expect(fetchMock.mock.calls[0][1]).toEqual(expect.objectContaining({
      method: 'POST',
      credentials: 'include',
      signal: controller.signal,
      body: expect.stringContaining('"stream":true'),
    }))
  })
})
