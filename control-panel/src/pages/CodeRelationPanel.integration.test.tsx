import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import CodeRelationPanel from './CodeRelationPanel'

const api = vi.hoisted(() => ({
  listCodeFiles: vi.fn(),
  listAllSymbols: vi.fn(),
  getCodeCallers: vi.fn(),
  getCodeCallees: vi.fn(),
}))

vi.mock('@/api/client', async importOriginal => ({
  ...await importOriginal<typeof import('@/api/client')>(),
  ...api,
}))

const symbol = {
  id: 's1',
  kind: 'function',
  name: 'route_request',
  qualified_name: 'gateway.route_request',
  file_path: 'src/gateway.py',
  language: 'python',
  start_line: 12,
  end_line: 20,
  signature: 'route_request()',
  docstring: null,
}

describe('CodeRelationPanel call graph explorer', () => {
  beforeEach(() => {
    api.listCodeFiles.mockReset().mockResolvedValue([
      { path: 'src/gateway.py', language: 'python', node_count: 1, size: 120 },
      { path: 'src/other.py', language: 'python', node_count: 0, size: 10 },
    ])
    api.listAllSymbols.mockReset().mockResolvedValue([symbol])
    api.getCodeCallers.mockReset().mockResolvedValue([
      { name: 'main', kind: 'function', file_path: 'src/main.py', start_line: 5 },
    ])
    api.getCodeCallees.mockReset().mockResolvedValue([
      { name: 'select_model', kind: 'function', file_path: 'src/model.py', start_line: 30 },
    ])
  })

  it('loads files, expands symbols, queries both relation directions and copies locations', async () => {
    const close = vi.fn()
    const user = userEvent.setup()
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    render(<CodeRelationPanel documentId="repo-1" onClose={close} />)

    expect(await screen.findByText(/gateway.py/)).toBeInTheDocument()
    expect(api.listCodeFiles).toHaveBeenCalledWith('repo-1')
    await user.click(screen.getByRole('button', { name: /gateway.py/ }))
    expect(await screen.findByText('route_request')).toBeInTheDocument()
    expect(api.listAllSymbols).toHaveBeenCalledWith('repo-1', { limit: 5000 })

    await user.click(screen.getByText('route_request'))
    expect(await screen.findByText('main')).toBeInTheDocument()
    expect(screen.getByText('select_model')).toBeInTheDocument()
    expect(api.getCodeCallers).toHaveBeenCalledWith('repo-1', 'route_request')
    expect(api.getCodeCallees).toHaveBeenCalledWith('repo-1', 'route_request')

    await user.click(screen.getByTitle('复制 src/gateway.py:12'))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('src/gateway.py:12'))
    await user.click(screen.getByTitle('收起'))
    expect(close).toHaveBeenCalled()
  })

  it('debounces symbol search and shows an honest empty result', async () => {
    const user = userEvent.setup()
    render(<CodeRelationPanel documentId="repo-1" onClose={vi.fn()} />)
    await screen.findByText(/gateway.py/)
    await user.type(screen.getByPlaceholderText('搜索文件或符号...'), 'missing')

    expect(await screen.findByText('没有找到匹配的文件')).toBeInTheDocument()
    await waitFor(() => expect(api.listAllSymbols).toHaveBeenCalledTimes(1))
  })

  it('surfaces file and symbol failures and retries them', async () => {
    api.listCodeFiles
      .mockRejectedValueOnce(new Error('files unavailable'))
      .mockResolvedValueOnce([{ path: 'src/gateway.py', language: 'python', node_count: 1, size: 120 }])
    api.listAllSymbols
      .mockRejectedValueOnce(new Error('symbols unavailable'))
      .mockResolvedValueOnce([symbol])
    const user = userEvent.setup()
    render(<CodeRelationPanel documentId="repo-1" onClose={vi.fn()} />)

    expect(await screen.findByText(/加载文件列表失败: files unavailable/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '重试' }))
    await user.click(await screen.findByRole('button', { name: /gateway.py/ }))
    expect(await screen.findByText(/加载符号失败: symbols unavailable/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '重试' }))
    expect(await screen.findByText('route_request')).toBeInTheDocument()
  })
})
