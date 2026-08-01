import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import Models from './Models'

const api = vi.hoisted(() => ({
  getFullConfig: vi.fn(),
  updateFullConfig: vi.fn(),
  testProviderConnectivity: vi.fn(),
  fetchProviderModels: vi.fn(),
}))
vi.mock('@/api/client', () => api)

beforeEach(() => {
  api.getFullConfig.mockResolvedValue({
    data: {
      providers: {
        openai: {
          api_key: '***',
          base_url: 'https://api.openai.com/v1',
          model_grouper: [{ models: [{ name: 'gpt-4o-mini', capabilities: ['text'] }], fallback_models: [], pricing: {} }],
          num_retries: 3,
          retry_after: 1000,
          timeout: 120,
        },
      },
      embedding: {},
    },
    message: 'success',
  })
  api.updateFullConfig.mockResolvedValue({ data: { updated: true }, message: 'success' })
  api.fetchProviderModels.mockResolvedValue({ data: { models: [] }, message: 'success' })
  api.testProviderConnectivity.mockReset()
})

it('shows testing progress and a visible success result', async () => {
  let resolveTest: (value: unknown) => void = () => undefined
  api.testProviderConnectivity.mockImplementationOnce(() => new Promise(resolve => { resolveTest = resolve }))
  const user = userEvent.setup()
  render(<Models />)

  const button = await screen.findByRole('button', { name: '测试 openai 连通性' })
  await user.click(button)
  expect(screen.getByRole('status')).toHaveTextContent('正在测试 openai 连通性')
  expect(button).toBeDisabled()

  resolveTest({ data: { success: true, latency_ms: 42 }, message: 'success' })
  expect(await screen.findByText('连接成功，延迟 42 ms')).toBeInTheDocument()
})

it('shows the provider error when connectivity fails', async () => {
  api.testProviderConnectivity.mockRejectedValueOnce(new Error('认证失败'))
  const user = userEvent.setup()
  render(<Models />)

  await user.click(await screen.findByRole('button', { name: '测试 openai 连通性' }))
  expect(await screen.findByText('连接失败：认证失败')).toBeInTheDocument()
})
