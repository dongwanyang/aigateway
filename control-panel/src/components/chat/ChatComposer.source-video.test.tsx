import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import ChatComposer from './ChatComposer'

describe('ChatComposer source-image video mode', () => {
  it('shows only motion and video timing controls', () => {
    render(
      <ChatComposer
        streaming={false}
        disabled={false}
        sourceImageMode
        onSend={vi.fn()}
        onStop={vi.fn()}
      />,
    )

    expect(screen.getByLabelText('视频时长')).toBeInTheDocument()
    expect(screen.getByPlaceholderText('描述主体动作或镜头运动')).toBeInTheDocument()
    expect(screen.queryByText('后端')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('图片模型/预设')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('选择参考图')).not.toBeInTheDocument()
  })

  it('submits only the supported source-video controls', async () => {
    const user = userEvent.setup()
    const onSend = vi.fn()
    render(
      <ChatComposer
        streaming={false}
        disabled={false}
        sourceImageMode
        onSend={onSend}
        onStop={vi.fn()}
      />,
    )

    await user.type(
      screen.getByPlaceholderText('描述主体动作或镜头运动'),
      '柯基跑向镜头',
    )
    fireEvent.change(screen.getByLabelText('视频时长'), {
      target: { value: '8' },
    })
    await user.click(screen.getByRole('button', { name: '发送' }))

    expect(onSend).toHaveBeenCalledWith('柯基跑向镜头', {
      generationOptions: {
        backend: 'local',
        prompt_mode: 'raw',
        quality: 'standard',
        duration_seconds: 8,
        fps: 8,
      },
    })
  })
})
