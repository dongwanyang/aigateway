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
    expect(fetchMock.mock.calls[0][1]).toEqual(expect.objectContaining({
      method: 'POST',
      signal: controller.signal,
      body: expect.stringContaining('"stream":true'),
    }))
  })
})

describe('draft and media workflow contracts', () => {
  it('maps successful draft responses into stable UI fields', async () => {
    vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse({ status: 'generating' }, 202))
      .mockResolvedValueOnce(jsonResponse({
        preview_data_url: 'data:image/png;base64,abc',
        preview_count: 2,
      }))
      .mockResolvedValueOnce(jsonResponse({
        result_data_url: 'data:image/png;base64,result',
      }))
      .mockResolvedValueOnce(jsonResponse({
        media_type: 'video',
        video_id: 'video-1',
        status: 'queued',
      }))
      .mockResolvedValueOnce(jsonResponse({
        upscaled_url: 'data:image/jpeg;base64,hires',
        target_resolution: [1920, 1080],
        algorithm: 'SUPIR',
      }))
      .mockResolvedValueOnce(jsonResponse({
        new_draft_id: 'draft-2',
        preview_url: '/admin/draft/draft-2/preview',
        attempt_number: 2,
        max_attempts: 5,
      }))
      .mockResolvedValueOnce(jsonResponse({
        status: 'pending',
        expires_at: 123,
        attempt_number: 2,
        max_attempts: 5,
      }))
      .mockResolvedValueOnce(jsonResponse({
        session_id: 'session-1',
        deleted_count: 3,
      }))
      .mockResolvedValueOnce(jsonResponse({
        id: 'video-1',
        status: 'completed',
      }))

    await expect(api.getDraftPreview('draft 1')).resolves.toEqual({ status: 'generating' })
    await expect(api.getDraftPreview('draft 1')).resolves.toEqual({
      previewDataUrl: 'data:image/png;base64,abc',
      previewCount: 2,
    })
    await expect(api.getDraftResult('draft 1')).resolves.toEqual({
      resultDataUrl: 'data:image/png;base64,result',
    })
    await expect(api.confirmDraft('draft 1')).resolves.toEqual({
      videoId: 'video-1',
      status: 'queued',
      mediaType: 'video',
    })
    await expect(api.confirmDraft('draft 1')).resolves.toEqual({
      upscaledUrl: 'data:image/jpeg;base64,hires',
      targetResolution: [1920, 1080],
      algorithm: 'SUPIR',
      mediaType: 'image',
    })
    await expect(api.rejectDraft('draft 1')).resolves.toEqual({
      newDraftId: 'draft-2',
      previewUrl: '/admin/draft/draft-2/preview',
      attemptNumber: 2,
      maxAttempts: 5,
    })
    await expect(api.getDraftStatus('draft 1')).resolves.toEqual({
      status: 'pending',
      expiresAt: 123,
      attemptNumber: 2,
      maxAttempts: 5,
    })
    await expect(api.deleteSessionDrafts('session/1')).resolves.toEqual({
      session_id: 'session-1',
      deleted_count: 3,
    })
    await expect(api.getVideoStatus('video/1')).resolves.toMatchObject({
      id: 'video-1',
      status: 'completed',
    })
  })
})

describe('admin REST client request construction', () => {
  it('sends representative CRUD bodies and encoded identifiers', async () => {
    const envelope = { data: {}, message: 'success' }
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(
      async () => jsonResponse(envelope),
    )

    await api.createChatCompletion({ model: 'm', messages: [] } as any)
    await api.listModels()
    await api.createEmbeddings({ model: 'e', input: ['x'] } as any)
    await api.listApiKeys(2, 30)
    await api.createApiKey({ user_id: 'alice' } as any)
    await api.deleteApiKey('key/a')
    await api.rotateApiKey('key/a')
    await api.updateApiKeyQuota('key/a', { daily_tokens: 10 })
    await api.getQuota('key/a')
    await api.updateL3EntryMode('point/a', 'auto', 12)
    await api.setPluginDebug('cache/a', true)
    await api.assignKeyGroup('key/a', 'grp/a', 'private')

    const calls = fetchMock.mock.calls.map(([url, init]) => ({
      url: String(url),
      method: (init as RequestInit | undefined)?.method ?? 'GET',
      body: (init as RequestInit | undefined)?.body,
    }))
    expect(calls).toEqual(expect.arrayContaining([
      expect.objectContaining({
        url: expect.stringContaining('/admin/api-keys?page=2&page_size=30'),
      }),
      expect.objectContaining({
        url: expect.stringContaining('/admin/api-keys/key%2Fa'),
        method: 'DELETE',
      }),
      expect.objectContaining({
        url: expect.stringContaining('/admin/cache/l3/entries/point%2Fa/mode'),
        method: 'PUT',
        body: JSON.stringify({ mode: 'auto', ttl_hours: 12 }),
      }),
      expect.objectContaining({
        url: expect.stringContaining('/admin/plugins/cache%2Fa/debug'),
        method: 'POST',
      }),
      expect.objectContaining({
        url: expect.stringContaining('/admin/api-keys/key%2Fa/group'),
        method: 'PUT',
        body: JSON.stringify({
          group_id: 'grp/a',
          cache_scope: 'private',
        }),
      }),
    ]))
  })
})
