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
  getGpuStatus: vi.fn(async () => ({
    data: {
      gateway: { available: false, allocated_bytes: 0, reserved_bytes: 0, device_used_bytes: 0, device_free_bytes: 0, device_total_bytes: 0 },
      comfyui: { available: false, memory: null },
      queue: { running: 0, pending: 0 },
      queue_idle: true,
      shared_gpu: false,
      diagnosis: [],
    },
    message: 'success',
  })),
  releaseGpuMemory: vi.fn(async () => ({ data: {}, message: 'success' })),
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
        return Response.json({
          data: {
            items: [{
              path: 'gpu_scheduler.comfyui_dynamic_vram_enabled',
              module: 'gpu_scheduler',
              description: '是否启用 ComfyUI Dynamic VRAM；修改后需重建 worker',
              value_type: 'boolean',
            }],
          },
          message: 'success',
        })
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
              gpu_scheduler: {},
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

  it('discards an invalid local draft when configuration is reloaded', async () => {
    const user = userEvent.setup()
    renderConfig()

    const pathCell = await screen.findByText('server.cors_origins')
    const row = pathCell.closest('tr')
    if (!row) throw new Error('server.cors_origins row not found')
    const editor = within(row).getByRole('textbox')

    fireEvent.focus(editor)
    fireEvent.change(editor, { target: { value: '[' } })
    expect(editor).toHaveValue('[')
    expect(screen.getByRole('button', { name: /保存配置/ })).toBeDisabled()

    await user.click(screen.getByRole('button', { name: /重新加载/ }))

    await waitFor(() => {
      expect(within(row).getByRole('textbox')).toHaveValue(
        '[\n  "http://localhost:5173"\n]',
      )
      expect(screen.getByRole('button', { name: /保存配置/ })).toBeDisabled()
    })
  })

  it('exposes Dynamic VRAM as a restart-required GPU setting', async () => {
    const user = userEvent.setup()
    renderConfig()

    const pathCell = await screen.findByText(
      'gpu_scheduler.comfyui_dynamic_vram_enabled',
    )
    const row = pathCell.closest('tr')
    if (!row) throw new Error('Dynamic VRAM row not found')
    const select = within(row).getByRole('combobox')
    expect(select).toHaveValue('false')

    await user.selectOptions(select, 'true')
    await user.click(screen.getByRole('button', { name: /保存配置/ }))

    await waitFor(() => {
      const call = vi.mocked(fetch).mock.calls.find(
        ([url, init]) => String(url).endsWith('/admin/config/table') && init?.method === 'PUT',
      )
      expect(call).toBeDefined()
      expect(
        JSON.parse(String(call?.[1]?.body)).gpu_scheduler
          .comfyui_dynamic_vram_enabled,
      ).toBe(true)
      expect(
        screen.getByText(/请重新运行 quickstart 或手工运行 GPU 拓扑控制器/),
      ).toBeInTheDocument()
    })
  })
})
