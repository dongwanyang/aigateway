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

  it.each([
    ['3', 3, '生成一段三秒视频'],
    ['8', 8, '生成一段八秒视频'],
  ] as const)(
    'sends an explicit %s-second duration and resets timing after submission',
    async (selectedValue, expectedDuration, prompt) => {
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

      const durationSelect = screen.getByRole('combobox', { name: '视频时长' })
      await user.selectOptions(durationSelect, selectedValue)
      await user.type(screen.getByPlaceholderText(/输入消息/), prompt)
      await user.keyboard('{Enter}')

      expect(onSend).toHaveBeenCalledWith(prompt, {
        generationOptions: {
          backend: 'auto',
          preset_id: undefined,
          quality: 'standard',
          prompt_mode: 'auto',
          width: undefined,
          height: undefined,
          duration_seconds: expectedDuration,
          fps: 8,
        },
        referenceImage: undefined,
      })
      expect(durationSelect).toHaveValue('5')
    },
  )
})
