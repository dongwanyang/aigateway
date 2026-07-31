import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import Config from './Config'

vi.mock('@/api/client', () => ({
  getComfyUIStatus: vi.fn(async () => ({
    data: { available: false, configuration_errors: [] },
    message: 'success',
  })),
  getGenerationPresets: vi.fn(async () => ({
    data: [],
    message: 'success',
  })),
}))

function renderConfig() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <Config />
    </QueryClientProvider>,
  )
}

describe('Config revision writes', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/admin/config/schema')) {
        return Response.json({ data: { items: [] }, message: 'success' })
      }
      if (url.endsWith('/admin/config/table') && init?.method === 'PUT') {
        return Response.json(
          { data: { updated: true }, message: 'success', revision: 'revision-2' },
          { headers: { ETag: '"revision-2"' } },
        )
      }
      if (url.endsWith('/admin/config')) {
        return Response.json(
          {
            data: {
              server: {
                host: '0.0.0.0',
                port: 8000,
                cors_origins: ['http://localhost:5173'],
              },
            },
            message: 'success',
            revision: 'revision-1',
          },
          { headers: { ETag: '"revision-1"' } },
        )
      }
      throw new Error(`unexpected request: ${url}`)
    }))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('sends the loaded revision in If-Match', async () => {
    const user = userEvent.setup()
    renderConfig()

    const pathCell = await screen.findByText('server.port')
    const row = pathCell.closest('tr')
    if (!row) throw new Error('server.port row not found')
    const input = within(row).getByRole('spinbutton')
    fireEvent.change(input, { target: { value: '9000' } })
    await user.click(screen.getByRole('button', { name: /保存配置/ }))

    await waitFor(() => {
      const call = vi.mocked(fetch).mock.calls.find(
        ([url, init]) => String(url).endsWith('/admin/config/table') && init?.method === 'PUT',
      )
      expect(call).toBeDefined()
      expect(call?.[1]?.headers).toEqual(
        expect.objectContaining({ 'If-Match': '"revision-1"' }),
      )
      expect(JSON.parse(String(call?.[1]?.body)).server.port).toBe(9000)
    })
  })

  it('preserves intermediate JSON and blocks stale saves', async () => {
    const user = userEvent.setup()
    renderConfig()

    const pathCell = await screen.findByText('server.cors_origins')
    const row = pathCell.closest('tr')
    if (!row) throw new Error('server.cors_origins row not found')
    const editor = within(row).getByRole('textbox')
    const save = screen.getByRole('button', { name: /保存配置/ })

    fireEvent.focus(editor)
    fireEvent.change(editor, { target: { value: '[' } })
    expect(editor).toHaveValue('[')
    expect(screen.queryByText(/JSON 格式无效/)).not.toBeInTheDocument()
    expect(save).toBeDisabled()

    fireEvent.change(editor, {
      target: { value: '["https://panel.example"]' },
    })
    expect(save).toBeEnabled()
    await user.click(save)

    await waitFor(() => {
      const call = vi.mocked(fetch).mock.calls.find(
        ([url, init]) => String(url).endsWith('/admin/config/table') && init?.method === 'PUT',
      )
      expect(call).toBeDefined()
      expect(JSON.parse(String(call?.[1]?.body)).server.cors_origins).toEqual([
        'https://panel.example',
      ])
    })
  })
})
