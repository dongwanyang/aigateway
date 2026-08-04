import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import ChatComposer from './ChatComposer'

describe('ChatComposer video timing', () => {
  it('keeps the five-second default implicit', async () => {
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

    expect(screen.getByRole('combobox', { name: '视频时长' })).toHaveValue('5')
    await user.type(screen.getByPlaceholderText(/输入消息/), '生成一段五秒视频')
    await user.keyboard('{Enter}')

    expect(onSend).toHaveBeenCalledWith('生成一段五秒视频')
  })

  it('sends an explicit duration and the supported default FPS', async () => {
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

    await user.selectOptions(
      screen.getByRole('combobox', { name: '视频时长' }),
      '8',
    )
    await user.type(screen.getByPlaceholderText(/输入消息/), '生成一段八秒视频')
    await user.keyboard('{Enter}')

    expect(onSend).toHaveBeenCalledWith('生成一段八秒视频', {
      generationOptions: {
        backend: 'auto',
        preset_id: undefined,
        quality: 'standard',
        prompt_mode: 'auto',
        width: undefined,
        height: undefined,
        duration_seconds: 8,
        fps: 8,
      },
      referenceImage: undefined,
    })
  })
})
