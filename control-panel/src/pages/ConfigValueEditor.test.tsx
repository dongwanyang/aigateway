import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { ConfigValueEditor } from './Config'
import type { ConfigRow } from './configEditor'

const row: ConfigRow = {
  path: 'providers.custom.model_grouper[0].models[0].features',
  group: 'providers',
  segments: [
    'providers',
    'custom',
    'model_grouper',
    0,
    'models',
    0,
    'features',
  ],
  value: ['tool_calling'],
  description: '运行时能力',
  schemaType: 'string[]',
  schemaEditor: 'token_list',
}

describe('ConfigValueEditor', () => {
  it('keeps a comma in the draft while the user types the next item', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn(() => true)
    const onValidityChange = vi.fn()
    render(
      <ConfigValueEditor
        row={row}
        onChange={onChange}
        onValidityChange={onValidityChange}
      />,
    )

    const input = screen.getByRole('textbox')
    await user.type(input, ', structured_output')

    expect(input).toHaveValue('tool_calling, structured_output')
    expect(onValidityChange).toHaveBeenCalledWith(row.path, false)
    expect(onValidityChange).toHaveBeenLastCalledWith(row.path, true)
    expect(onChange).toHaveBeenLastCalledWith(
      row,
      'tool_calling, structured_output',
    )
  })

  it('keeps an incomplete trailing item invalid after blur', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn(() => false)
    const onValidityChange = vi.fn()
    render(
      <ConfigValueEditor
        row={row}
        onChange={onChange}
        onValidityChange={onValidityChange}
      />,
    )

    const input = screen.getByRole('textbox')
    await user.type(input, ',')
    await user.tab()

    expect(input).toHaveValue('tool_calling,')
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(onValidityChange).toHaveBeenLastCalledWith(row.path, false)
  })
})
