import { act, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import AuthGuard from './AuthGuard'
import CapabilityBanner from './CapabilityBanner'
import CapabilityUnavailable from './CapabilityUnavailable'
import ErrorBoundary from './ErrorBoundary'
import PageErrorBoundary from './PageErrorBoundary'
import ChatComposer from './chat/ChatComposer'
import ChatTimeline from './chat/ChatTimeline'
import DraftCard from './chat/DraftCard'
import ImageLightbox from './chat/ImageLightbox'
import MediaImage from './chat/MediaImage'
import MediaVideo from './chat/MediaVideo'
import MessageBubble, { classifyContent } from './chat/MessageBubble'
import SessionList from './chat/SessionList'

const auth = vi.hoisted(() => ({
  state: { isAuthenticated: true },
  forceReset: false,
  isLoading: false,
}))
const capabilities = vi.hoisted(() => ({ data: undefined as unknown }))
const getVideoStatus = vi.hoisted(() => vi.fn())

vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => auth }))
vi.mock('@/hooks/useRuntimeCapabilities', () => ({ useRuntimeCapabilities: () => capabilities }))
vi.mock('@/api/client', async importOriginal => ({
  ...await importOriginal<typeof import('@/api/client')>(),
  getVideoStatus,
}))

describe('shared UI components', () => {
  beforeEach(() => {
    auth.state.isAuthenticated = true
    auth.forceReset = false
    auth.isLoading = false
    capabilities.data = undefined
    getVideoStatus.mockReset()
  })

  it('enforces authentication and forced-reset redirects', () => {
    auth.state.isAuthenticated = false
    const { rerender } = render(
      <MemoryRouter initialEntries={['/private']}>
        <Routes>
          <Route path="/private" element={<AuthGuard><div>private</div></AuthGuard>} />
          <Route path="/login" element={<div>login destination</div>} />
        </Routes>
      </MemoryRouter>,
    )
    expect(screen.getByText('login destination')).toBeInTheDocument()

    auth.state.isAuthenticated = true
    auth.forceReset = true
    rerender(
      <MemoryRouter initialEntries={['/private']}>
        <Routes>
          <Route path="/private" element={<AuthGuard><div>private</div></AuthGuard>} />
          <Route path="/login" element={<div>reset destination</div>} />
        </Routes>
      </MemoryRouter>,
    )
    expect(screen.getByText('reset destination')).toBeInTheDocument()
  })

  it('shows guarded loading and authenticated content states', () => {
    auth.isLoading = true
    const { rerender } = render(
      <MemoryRouter><AuthGuard><div>protected content</div></AuthGuard></MemoryRouter>,
    )
    expect(document.querySelector('.animate-spin')).toBeInTheDocument()
    auth.isLoading = false
    auth.state.isAuthenticated = true
    rerender(<MemoryRouter><AuthGuard><div>protected content</div></AuthGuard></MemoryRouter>)
    expect(screen.getByText('protected content')).toBeInTheDocument()
  })

  it('renders only genuinely unavailable runtime capabilities', () => {
    capabilities.data = {
      profile: 'core',
      capabilities: {
        rag: { available: false, reason: '缺少向量库' },
        vision: { available: true },
      },
    }
    const { rerender } = render(<CapabilityBanner />)
    expect(screen.getByText(/受限能力：知识库/)).toBeInTheDocument()

    capabilities.data = {
      profile: 'full',
      capabilities: { rag: { available: true }, vision: { available: true } },
    }
    rerender(<CapabilityBanner />)
    expect(screen.queryByText(/受限能力/)).not.toBeInTheDocument()
  })

  it('explains a missing capability with its install command', () => {
    render(<CapabilityUnavailable
      title="知识库不可用"
      description="当前镜像未安装该能力"
      capability={{
        installed: false,
        configured: false,
        available: false,
        reason: '缺少 extra',
        install_command: 'pip install extra',
      }}
    />)
    expect(screen.getByText('当前状态：缺少 extra')).toBeInTheDocument()
    expect(screen.getByText('pip install extra')).toBeInTheDocument()
  })

  it('captures application and page render failures', async () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const Broken = () => { throw new Error('真实渲染异常') }
    const reload = vi.fn()
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...window.location, reload },
    })
    const user = userEvent.setup()
    const root = render(<ErrorBoundary><Broken /></ErrorBoundary>)
    expect(screen.getByText('真实渲染异常')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '重新加载' }))
    expect(reload).toHaveBeenCalled()
    root.unmount()

    render(<PageErrorBoundary><Broken /></PageErrorBoundary>)
    expect(screen.getByText('页面加载失败')).toBeInTheDocument()
    expect(errorSpy).toHaveBeenCalled()
    errorSpy.mockRestore()
  })

  it('sends trimmed composer input, preserves Shift+Enter, and stops a stream', async () => {
    const onSend = vi.fn()
    const onStop = vi.fn()
    const user = userEvent.setup()
    const { rerender } = render(<ChatComposer streaming={false} disabled={false} onSend={onSend} onStop={onStop} />)
    const input = screen.getByPlaceholderText(/输入消息/)
    await user.type(input, '  hello  ')
    await user.keyboard('{Enter}')
    expect(onSend).toHaveBeenCalledWith('hello')
    expect(input).toHaveValue('')

    rerender(<ChatComposer streaming disabled={false} onSend={onSend} onStop={onStop} />)
    await user.click(screen.getByRole('button', { name: '停止' }))
    expect(onStop).toHaveBeenCalled()
  })

  it('handles image loading, lightbox controls and keyboard close', async () => {
    const user = userEvent.setup()
    const { rerender } = render(<MediaImage content="base64bytes" done={false} />)
    expect(screen.getByText(/生成图片中/)).toBeInTheDocument()
    rerender(<MediaImage content="base64bytes" done />)
    const generated = screen.getByAltText('生成图片')
    expect(generated).toHaveAttribute('src', 'data:image/jpeg;base64,base64bytes')
    fireEvent.error(generated)
    expect(screen.getByText('图片加载失败')).toBeInTheDocument()

    rerender(<ImageLightbox src="data:image/png;base64,x" alt="结果图" />)
    await user.click(screen.getByAltText('结果图'))
    expect(screen.getByRole('dialog', { name: '结果图' })).toBeInTheDocument()
    await user.click(screen.getByTitle('放大'))
    expect(screen.getByText('130%')).toBeInTheDocument()
    await user.click(screen.getByTitle('重置'))
    expect(screen.getByText('100%')).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('renders draft states and wires confirm/reject actions', async () => {
    const confirm = vi.fn()
    const reject = vi.fn()
    const user = userEvent.setup()
    const base = { draftId: 'd1', previewUrl: '/preview', mediaType: 'image' as const }
    const { rerender } = render(<DraftCard
      draft={{ ...base, status: 'pending', previewDataUrl: 'data:image/png;base64,x' }}
      onConfirm={confirm}
      onReject={reject}
    />)
    await user.click(screen.getByRole('button', { name: /确认放大/ }))
    await user.click(screen.getByRole('button', { name: /重新生成/ }))
    expect(confirm).toHaveBeenCalled()
    expect(reject).toHaveBeenCalled()

    rerender(<DraftCard draft={{ ...base, status: 'error', errorMessage: '上游失败' }} onConfirm={confirm} onReject={reject} />)
    expect(screen.getByText(/操作失败.*上游失败/)).toBeInTheDocument()
    rerender(<DraftCard draft={{ ...base, status: 'confirmed', resultLost: true }} onConfirm={confirm} onReject={reject} />)
    expect(screen.getByText(/高清图未缓存/)).toBeInTheDocument()
  })

  it('classifies and renders user, streaming, interrupted and draft messages', async () => {
    expect(classifyContent(null, 'https://example.test/a.png')).toBe('image')
    expect(classifyContent(null, 'id=v1 poll /v1/videos/v1')).toBe('video')
    expect(classifyContent(null, 'plain')).toBe('text')

    const base = { id: 'm1', ts: 1 }
    const { rerender } = render(<MessageBubble
      msg={{ ...base, role: 'user', content: 'question' }}
      isStreaming={false}
      pendingAssistantId={null}
    />)
    expect(screen.getByText('question')).toBeInTheDocument()
    rerender(<MessageBubble
      msg={{ ...base, role: 'assistant', content: 'partial', incomplete: true }}
      isStreaming={false}
      pendingAssistantId={null}
    />)
    expect(screen.getByText(/已中断/)).toBeInTheDocument()
    rerender(<MessageBubble
      msg={{ ...base, role: 'assistant', content: '', intent: 'understanding' }}
      isStreaming
      pendingAssistantId="m1"
    />)
    expect(screen.getByLabelText('正在思考').querySelectorAll('span')).toHaveLength(3)

    const confirm = vi.fn()
    rerender(<MessageBubble
      msg={{
        ...base,
        role: 'assistant',
        content: '',
        draft: {
          draftId: 'draft-1',
          previewUrl: '/preview',
          mediaType: 'image',
          status: 'pending',
          previewDataUrl: 'data:image/png;base64,x',
        },
      }}
      isStreaming={false}
      pendingAssistantId={null}
      onConfirmDraft={confirm}
    />)
    await userEvent.click(screen.getByRole('button', { name: /确认放大/ }))
    expect(confirm).toHaveBeenCalledWith('m1')
  })

  it('polls a completed video task and rejects unsafe result URLs', async () => {
    getVideoStatus.mockResolvedValueOnce({ status: 'completed', metadata: { url: 'https://cdn.test/movie.mp4' } })
    vi.useFakeTimers()
    const { rerender } = render(<MediaVideo content="id=vid-1 poll /v1/videos/vid-1" done />)
    await act(async () => { await vi.advanceTimersByTimeAsync(3000) })
    expect(document.querySelector('video')).toHaveAttribute('src', 'https://cdn.test/movie.mp4')

    getVideoStatus.mockResolvedValueOnce({ status: 'completed', url: 'javascript:alert(1)' })
    rerender(<MediaVideo content="id=vid-2 poll /v1/videos/vid-2" done />)
    await act(async () => { await vi.advanceTimersByTimeAsync(3000) })
    expect(screen.getByText('视频 URL 无效')).toBeInTheDocument()

    getVideoStatus.mockResolvedValueOnce({ status: 'failed', error: { message: 'encoder failed' } })
    rerender(<MediaVideo content="id=vid-3 poll /v1/videos/vid-3" done />)
    await act(async () => { await vi.advanceTimersByTimeAsync(3000) })
    expect(screen.getByText('视频生成失败')).toBeInTheDocument()
    vi.useRealTimers()
  })

  it('renders video submission and parse failures without polling', () => {
    const { rerender } = render(<MediaVideo content="" done={false} />)
    expect(screen.getByText(/提交视频任务中/)).toBeInTheDocument()
    rerender(<MediaVideo content="ordinary text" done />)
    expect(screen.getByText('无法解析视频任务 id')).toBeInTheDocument()
    expect(getVideoStatus).not.toHaveBeenCalled()
  })

  it('selects, deletes and creates chat sessions through distinct controls', async () => {
    const onNew = vi.fn()
    const onSelect = vi.fn()
    const onDelete = vi.fn()
    const user = userEvent.setup()
    render(<SessionList
      sessions={[
        { id: 's1', title: 'Recent', messages: [], createdAt: Date.now(), updatedAt: Date.now() },
        { id: 's2', title: 'Yesterday', messages: [], createdAt: 1, updatedAt: Date.now() - 25 * 60 * 60 * 1000 },
      ]}
      activeId="s1"
      onNew={onNew}
      onSelect={onSelect}
      onDelete={onDelete}
    />)
    await user.click(screen.getByRole('button', { name: /新对话/ }))
    await user.click(screen.getByText('Yesterday'))
    await user.click(screen.getAllByTitle('删除会话')[0])
    expect(onNew).toHaveBeenCalled()
    expect(onSelect).toHaveBeenCalledWith('s2')
    expect(onDelete).toHaveBeenCalledWith('s1')
    expect(screen.getByText('1 天前')).toBeInTheDocument()
  })

  it('tracks whether the chat timeline is near the bottom before auto-scrolling', () => {
    const { container, rerender } = render(<ChatTimeline
      messages={[]}
      streaming={false}
      streamingId={null}
      pendingAssistantId={null}
    />)
    const timeline = container.firstElementChild as HTMLDivElement
    Object.defineProperties(timeline, {
      scrollHeight: { configurable: true, value: 1000 },
      scrollTop: { configurable: true, value: 100 },
      clientHeight: { configurable: true, value: 400 },
    })
    fireEvent.scroll(timeline)
    rerender(<ChatTimeline
      messages={[{ id: 'm-scroll', role: 'user', content: 'new message', ts: 1 }]}
      streaming
      streamingId={null}
      pendingAssistantId={null}
    />)
    expect(screen.getByText('new message')).toBeInTheDocument()
  })
})
