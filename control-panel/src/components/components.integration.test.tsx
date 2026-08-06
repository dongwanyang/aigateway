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

  it('discovers image presets, selects Qwen explicitly, and refreshes models', async () => {
    const onSend = vi.fn()
    const onRefreshPresets = vi.fn()
    const user = userEvent.setup()
    render(
      <ChatComposer
        streaming={false}
        disabled={false}
        onSend={onSend}
        onStop={vi.fn()}
        onRefreshPresets={onRefreshPresets}
        presets={[
          {
            id: 'qwen-image',
            name: 'Qwen-Image 中文/英文图片',
            kind: 'image',
            builtin: true,
            source: 'builtin',
            selectable: true,
            enabled: true,
            languages: ['zh', 'en'],
            validation: { missing_models: [], missing_nodes: [] },
          },
          {
            id: 'checkpoint.bG9jYWw',
            name: 'local（本地 Checkpoint）',
            kind: 'image',
            builtin: false,
            source: 'discovered',
            selectable: true,
            enabled: true,
            languages: ['zh', 'en'],
            validation: { missing_models: [], missing_nodes: [] },
          },
          {
            id: 'missing-model',
            name: '缺失模型',
            kind: 'image',
            builtin: false,
            source: 'custom',
            selectable: false,
            enabled: true,
            languages: ['en'],
            validation: { missing_models: ['checkpoints/missing.safetensors'], missing_nodes: [] },
          },
        ]}
      />,
    )

    const modelSelect = screen.getByRole('combobox', { name: '图片模型/预设' })
    expect(screen.getByRole('option', { name: /local.*已安装/ })).toBeEnabled()
    expect(screen.getByRole('option', { name: /缺失模型.*不可用/ })).toBeDisabled()
    await user.selectOptions(modelSelect, 'qwen-image')
    expect(screen.getByRole('combobox', { name: /后端/ })).toHaveValue('local')
    await user.type(screen.getByPlaceholderText(/输入消息/), '生成一张海报')
    await user.keyboard('{Enter}')

    expect(onSend).toHaveBeenCalledWith('生成一张海报', {
      generationOptions: {
        backend: 'local',
        preset_id: 'qwen-image',
        quality: 'standard',
        prompt_mode: 'auto',
        width: undefined,
        height: undefined,
      },
    })
    await user.click(screen.getByRole('button', { name: '刷新图片模型' }))
    expect(onRefreshPresets).toHaveBeenCalledTimes(1)
  })

  it('uploads and sends a reference image for img2img or img2video', async () => {
    const onSend = vi.fn()
    const user = userEvent.setup()
    render(
      <ChatComposer
        streaming={false}
        disabled={false}
        onSend={onSend}
        onStop={vi.fn()}
      />,
    )
    const file = new File(['reference-image'], 'golden.png', {
      type: 'image/png',
    })

    await user.upload(screen.getByLabelText('上传参考图'), file)
    expect(await screen.findByRole('img', { name: '参考图预览' })).toBeVisible()
    await user.type(
      screen.getByPlaceholderText(/输入消息/),
      '让这只狗在草地上奔跑',
    )
    await user.keyboard('{Enter}')

    expect(onSend).toHaveBeenCalledWith(
      '让这只狗在草地上奔跑',
      {
        referenceImage: expect.objectContaining({
          name: 'golden.png',
          mimeType: 'image/png',
          size: file.size,
          dataUrl: expect.stringMatching(/^data:image\/png;base64,/),
        }),
      },
    )
    expect(screen.queryByRole('img', { name: '参考图预览' })).not.toBeInTheDocument()
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
    await user.click(screen.getByRole('button', { name: /确认生成高清图/ }))
    await user.click(screen.getByRole('button', { name: /重新生成/ }))
    expect(confirm).toHaveBeenCalled()
    expect(reject).toHaveBeenCalled()

    rerender(<DraftCard draft={{ ...base, status: 'error', errorMessage: '上游失败' }} onConfirm={confirm} onReject={reject} />)
    expect(screen.getByText(/操作失败.*上游失败/)).toBeInTheDocument()
    rerender(<DraftCard draft={{ ...base, status: 'confirmed', resultLost: true }} onConfirm={confirm} onReject={reject} />)
    expect(screen.getByText(/高清图未缓存/)).toBeInTheDocument()
  })

  it('renders indeterminate progress for running backend work', () => {
    render(<DraftCard
      draft={{
        draftId: 'd-progress',
        previewUrl: '/preview',
        mediaType: 'image',
        status: 'running',
        stage: 'comfyui.workflow_submitted',
        progress: 0.42,
      }}
      onConfirm={vi.fn()}
      onReject={vi.fn()}
    />)

    expect(screen.getByText(/ComfyUI 正在生成草稿预览.*comfyui.workflow_submitted/)).toBeInTheDocument()
    expect(screen.queryByText(/42%/)).not.toBeInTheDocument()
    expect(screen.getByRole('progressbar', { name: /草稿生成进度/ })).not.toHaveAttribute('aria-valuenow')
  })

  it('renders real ComfyUI sampling progress when provided by backend', () => {
    render(<DraftCard
      draft={{
        draftId: 'd-real-progress',
        previewUrl: '/preview',
        mediaType: 'image',
        status: 'running',
        stage: 'sampling 6/12',
        progress: 0.6,
        progressSource: 'comfyui',
      }}
      onConfirm={vi.fn()}
      onReject={vi.fn()}
    />)

    expect(screen.getByText(/60%.*采样 6\/12/)).toBeInTheDocument()
    expect(screen.getByRole('progressbar', { name: /草稿生成进度/ })).toHaveAttribute('aria-valuenow', '60')
  })

  it('renders the finalizing phase without claiming sampling is complete output', () => {
    render(<DraftCard
      draft={{
        draftId: 'd-finalizing',
        previewUrl: '/preview',
        mediaType: 'image',
        status: 'refining',
        stage: 'finalizing',
        progress: 1,
        progressSource: 'stage',
      }}
      onConfirm={vi.fn()}
      onReject={vi.fn()}
    />)

    expect(screen.getByText(/正在解码并保存/)).toBeInTheDocument()
    expect(screen.getByText(/正在解码并保存/)).not.toHaveTextContent(/100%/)
    expect(screen.getByRole('progressbar', { name: /草稿生成进度/ })).not.toHaveAttribute('aria-valuenow')
  })

  it('resets to the ComfyUI node progress when a new node starts', () => {
    render(<DraftCard
      draft={{
        draftId: 'd-node-loading',
        previewUrl: '/preview',
        mediaType: 'image',
        status: 'running',
        stage: 'executing 12',
        progress: 0,
        progressSource: 'comfyui',
      }}
      onConfirm={vi.fn()}
      onReject={vi.fn()}
    />)

    expect(screen.getByText(/0%.*ComfyUI 节点 12 执行中/)).toBeInTheDocument()
    expect(screen.getByRole('progressbar', { name: /草稿生成进度/ })).toHaveAttribute('aria-valuenow', '0')
  })

  it('renders a local ComfyUI video result without an Agnes video id', () => {
    render(<DraftCard
      draft={{
        draftId: 'video-draft',
        previewUrl: '/preview',
        mediaType: 'video',
        status: 'confirmed',
        resultDataUrl: 'data:video/mp4;base64,AAAA',
      }}
      onConfirm={vi.fn()}
      onReject={vi.fn()}
    />)

    expect(screen.getByText(/视频已生成/)).toBeInTheDocument()
    expect(document.querySelector('video')).toHaveAttribute(
      'src',
      'data:video/mp4;base64,AAAA',
    )
    expect(screen.getByRole('button', { name: /确认生成视频/ })).toBeDisabled()
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
    await userEvent.click(screen.getByRole('button', { name: /确认生成高清图/ }))
    expect(confirm).toHaveBeenCalledWith('m1')
  })

  it('polls a completed video task and rejects unsafe result URLs', async () => {
    getVideoStatus.mockResolvedValueOnce({ status: 'completed', metadata: { url: 'https://cdn.test/movie.mp4' } })
    vi.useFakeTimers()
    const completed = render(<MediaVideo content="id=vid-1 poll /v1/videos/vid-1" done />)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(document.querySelector('video')).toHaveAttribute('src', 'https://cdn.test/movie.mp4')
    completed.unmount()

    getVideoStatus.mockResolvedValueOnce({ status: 'completed', url: 'javascript:alert(1)' })
    const unsafe = render(<MediaVideo content="id=vid-2 poll /v1/videos/vid-2" done />)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(screen.getByText('视频 URL 无效')).toBeInTheDocument()
    unsafe.unmount()

    getVideoStatus.mockResolvedValueOnce({ status: 'failed', error: { message: 'encoder failed' } })
    const failed = render(<MediaVideo content="id=vid-3 poll /v1/videos/vid-3" done />)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(screen.getByText('视频生成失败')).toBeInTheDocument()
    failed.unmount()
    vi.useRealTimers()
  })

  it('renders video submission and parse failures without polling', () => {
    const { rerender } = render(<MediaVideo content="" done={false} />)
    expect(screen.getByText(/提交视频任务中/)).toBeInTheDocument()
    rerender(<MediaVideo content="ordinary text" done />)
    expect(screen.getByText('无法解析视频任务 id')).toBeInTheDocument()
    expect(getVideoStatus).not.toHaveBeenCalled()
  })

  it('renders a known video URL immediately without polling', () => {
    // 回归:之前只把 URL 写进 state 而不推进 phase,已完成的视频会一直显示
    // "生成视频中",并且还会为一个已有结果的任务重新发起轮询。
    render(<MediaVideo
      content=""
      videoId="vid-known"
      videoUrl="https://cdn.test/done.mp4"
      done
    />)
    expect(document.querySelector('video')).toHaveAttribute('src', 'https://cdn.test/done.mp4')
    expect(screen.queryByText(/生成视频中/)).not.toBeInTheDocument()
    expect(getVideoStatus).not.toHaveBeenCalled()
  })

  it('restores a persisted terminal video phase without polling', () => {
    render(<MediaVideo content="" videoId="vid-failed" videoPhase="failed" done />)
    expect(screen.getByText('视频生成失败')).toBeInTheDocument()
    expect(getVideoStatus).not.toHaveBeenCalled()
  })

  it('reports the shared polling budget rather than a shorter private timeout', async () => {
    // 回归:组件曾用 120s 私有超时,远小于 30 分钟的轮询预算,
    // 后端仍在生成的任务会被前端提前判成超时。
    getVideoStatus.mockResolvedValue({ status: 'in_progress' })
    vi.useFakeTimers()
    render(<MediaVideo content="id=vid-slow poll /v1/videos/vid-slow" done />)
    await act(async () => { await vi.advanceTimersByTimeAsync(130_000) })
    expect(screen.getByText(/生成视频中/)).toBeInTheDocument()
    expect(screen.queryByText(/视频生成超时/)).not.toBeInTheDocument()
    vi.useRealTimers()
  })

  it('shows an errored video message as text instead of the video renderer', () => {
    expect(classifyContent('generation:video', '视频生成失败：encoder failed', true)).toBe('text')
    render(<MessageBubble
      msg={{
        id: 'm-video-error',
        ts: 1,
        role: 'assistant',
        content: '视频生成失败：encoder failed',
        intent: 'generation:video',
        error: true,
        videoId: 'vid-err',
        videoPhase: 'failed',
      }}
      isStreaming={false}
      pendingAssistantId={null}
    />)
    expect(screen.getByText(/视频生成失败：encoder failed/)).toBeInTheDocument()
  })

  it('shows indeterminate progress for stage-sourced generating drafts', () => {
    // 回归:后端把 generating 阶段的 progress 固定写成 0.1,按真实百分比渲染
    // 会让进度条整段生成期间静止在 10%。
    render(<DraftCard
      draft={{
        draftId: 'd-generating',
        previewUrl: '/preview',
        mediaType: 'image',
        status: 'generating',
        stage: 'running',
        progress: 0.1,
        progressSource: 'stage',
      }}
      onConfirm={vi.fn()}
      onReject={vi.fn()}
    />)
    expect(screen.queryByText(/10%/)).not.toBeInTheDocument()
    expect(screen.getByRole('progressbar', { name: /草稿生成进度/ })).not.toHaveAttribute('aria-valuenow')
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
